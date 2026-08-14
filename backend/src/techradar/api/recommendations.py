"""記事起点推薦と Discover フィードの API（`PROJECT_SPEC.md` §6.1, §13, §20）。

推薦の生成・保存自体は `recommendation/service.py`（T1〜T3）に委ね、ここでは
リクエストの受け口・レスポンス整形・検索/絞り込み条件の受け渡し・番号付き
ページング（Issue #90）だけを担う。
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.api.deps import get_app_settings, get_current_user_id, get_now, get_session
from techradar.api.feedback import ArticleFeedbackResponse
from techradar.api.rate_limit import RATE_LIMITED_RESPONSES, enforce_recommendation_rate_limit
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, Recommendation, UserArticle
from techradar.db.enums import FeedbackAction, RecommendationMode
from techradar.recommendation.config import get_scoring_config
from techradar.recommendation.service import (
    READ_ORIGIN_VALUES,
    FeedFilters,
    compute_filter_fingerprint,
    count_recommendations,
    find_latest_run,
    generate_recommendations,
    load_recommendation_page,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["recommendations"])

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]
NowDep = Annotated[datetime, Depends(get_now)]

# GET /api/feed の limit の既定値・上限値は config/scoring.yaml の limits に従う
# （運用しながら調整するため、コードに埋め込まない）。
_page_size_limits = get_scoring_config().limits
DEFAULT_PAGE_SIZE = _page_size_limits.default_page_size
MAX_PAGE_SIZE = _page_size_limits.max_page_size


class RecommendationItem(BaseModel):
    """推薦結果 1 件のレスポンス。

    記事本文（`Article.body`）は含めない（ADR 0001、外部には表示しない）。
    """

    article_id: uuid.UUID
    canonical_url: str
    original_url: str
    title: str
    translated_title: str | None
    summary_ja: str | None
    source_domain: str
    language: str | None
    published_at: datetime | None
    is_primary_source: bool
    topics: list[str]
    technologies: list[str]
    score: float
    rank: int
    # `recommendations.reasons`（JSONB）をそのまま返す。スコア内訳全項目と
    # 機械生成の `summary` を含む（`ranking.ScoreBreakdown.to_reasons`）。
    reasons: dict[str, float | str]
    # `user_articles` の origin が read_full/clicked のいずれかなら true
    # （`recommendation/service.py` の `READ_ORIGIN_VALUES` と同一基準、
    # `PROJECT_SPEC.md` §6.1「既読記事の再表示は抑制する」）。
    is_read: bool
    # `article_feedback` の現在行（Good/Bad/保存の最新の意思表示）。未設定なら null。
    feedback: ArticleFeedbackResponse | None


class ArticleRecommendationsResponse(BaseModel):
    """記事起点推薦（`POST /api/articles/{article_id}/recommendations`）のレスポンス。"""

    run_id: uuid.UUID
    mode: str
    generated_at: datetime
    items: list[RecommendationItem]


class FeedResponse(BaseModel):
    """Discover フィード（`GET /api/feed`）のレスポンス（Issue #90）。

    ページングは番号付きページ（`page` / `page_size`）に統一する。
    `total_count` は絞り込み後に run へ保存された件数（`count_recommendations`）で、
    `limits.feed_run_size`（`config/scoring.yaml`）を超える候補は元々対象外になる
    （決定済みの設計）。
    """

    items: list[RecommendationItem]
    # run に保存された総件数（Bad 除外前。`_build_items` の Bad 除外は表示時だけの
    # ものなので `total_count` には影響しない）。
    total_count: int
    # 要求されたページ番号（1 始まり）。範囲外でもそのまま返す。
    page: int
    # 1 ページあたりの件数（`limit` クエリパラメータの値）。
    page_size: int
    # `total_count` を `page_size` で割って切り上げた総ページ数。0 件なら 0。
    total_pages: int


def _build_item(
    recommendation: Recommendation,
    article: Article,
    *,
    is_read: bool,
    feedback: ArticleFeedback | None,
) -> RecommendationItem:
    """DB から読み出した `Recommendation` / `Article` を API レスポンス項目にする。

    `is_read` / `feedback` はページ内の全記事ぶんをまとめて 1 回ずつのクエリで
    引いた辞書から呼び出し側（`_build_items`）が渡す。ここではその組み立てだけを行う。
    """
    return RecommendationItem(
        article_id=article.id,
        canonical_url=article.canonical_url,
        original_url=article.original_url,
        title=article.title,
        translated_title=article.translated_title,
        summary_ja=article.summary_ja,
        source_domain=article.source_domain,
        language=article.language,
        published_at=article.published_at,
        is_primary_source=article.is_primary_source,
        topics=list(article.topics),
        technologies=list(article.technologies),
        score=recommendation.score,
        rank=recommendation.rank,
        reasons=recommendation.reasons,
        is_read=is_read,
        feedback=(
            ArticleFeedbackResponse.model_validate(feedback) if feedback is not None else None
        ),
    )


def _build_items(
    session: Session,
    user_id: uuid.UUID,
    rows: Sequence[tuple[Recommendation, Article]],
) -> list[RecommendationItem]:
    """ページ内の `Recommendation` / `Article` 行から API レスポンス項目一覧を組み立てる。

    `user_articles`（is_read 判定）と `article_feedback`（feedback）を、記事ごとに
    クエリを撒く N+1 にせず、対象記事 ID をまとめて 1 回ずつのクエリで引く。
    """
    if not rows:
        return []

    article_ids = [article.id for _, article in rows]

    origins_by_article_id: dict[uuid.UUID, set[str]] = {}
    for article_id, origin in session.execute(
        select(UserArticle.article_id, UserArticle.origin).where(
            UserArticle.user_id == user_id, UserArticle.article_id.in_(article_ids)
        )
    ).all():
        origins_by_article_id.setdefault(article_id, set()).add(origin)

    feedback_by_article_id = {
        feedback.article_id: feedback
        for feedback in session.scalars(
            select(ArticleFeedback).where(
                ArticleFeedback.user_id == user_id, ArticleFeedback.article_id.in_(article_ids)
            )
        ).all()
    }

    # Bad 済み記事をレスポンスから除外する（PROJECT_SPEC.md §6.1「既に Bad した
    # 記事は再表示しない」、Issue #13）。Bad による候補除外は本来
    # `recommendation/service.py` の `load_candidates` が新規 run を作るときにしか
    # 効かない。`GET /api/feed` の cursor 省略時は直近の DISCOVER run を最大
    # `feed_run_reuse_seconds`（`config/scoring.yaml`）秒まで再利用するため
    # （`_resolve_discover_run_id`）、再利用ウィンドウ内で付けた Bad は
    # `recommendations` 行として残り続ける run には反映されない。そのため
    # ページ組み立てのこの時点で改めて除外する。記事起点推薦
    # （`create_article_recommendations`）は生成直後の run を読むため元々 Bad は
    # 含まれないが、同じ組み立て関数を経由するため挙動は変わらない。
    return [
        _build_item(
            recommendation,
            article,
            is_read=bool(origins_by_article_id.get(article.id, set()) & READ_ORIGIN_VALUES),
            feedback=feedback_by_article_id.get(article.id),
        )
        for recommendation, article in rows
        if feedback_by_article_id.get(article.id) is None
        or feedback_by_article_id[article.id].action != FeedbackAction.BAD.value
    ]


def _resolve_discover_run_id(
    session: Session,
    settings: Settings,
    user_id: uuid.UUID,
    now: datetime,
    feed_filters: FeedFilters,
) -> uuid.UUID:
    """`GET /api/feed` が使う run の id を決める（Issue #90）。

    `feed_filters` から計算したフィンガープリント（`compute_filter_fingerprint`）が
    一致する直近の DISCOVER run が再利用してよい時間内（`config/scoring.yaml` の
    `limits.feed_run_reuse_seconds`）なら新規生成せずその run を再利用し、
    そうでなければ新規生成する。`feed_run_reuse_seconds` が 0 の場合は常に
    新規生成する（無効化）。絞り込み無し（`FeedFilters()`）でも同じ規則で判定する
    （空条件のフィンガープリントで一致させる）。

    絞り込みは表示中の run だけを絞るのではなく、`generate_recommendations` が
    候補読み込みの時点から絞り込んで推薦を作り直す（決定済みの設計）。

    直近 run の読み取りと生成の間に排他制御は掛けないため、ほぼ同時に届いた
    同一条件のリクエストは、いずれも「再利用できる run が無い」と判断して
    それぞれ run を作りうる。単一ユーザー・ローカル実行の前提では実害が小さい
    ため許容する。古い run は `jobs/handlers/purge_recommendation_runs.py` が
    保持期間超過分を削除し、この関数自体の呼び出し過多は `rate_limit.py` の
    レート制限（Issue #28）が抑える。
    """
    reuse_seconds = get_scoring_config().limits.feed_run_reuse_seconds
    if reuse_seconds > 0:
        fingerprint = compute_filter_fingerprint(feed_filters)
        latest_run = find_latest_run(
            session, user_id, RecommendationMode.DISCOVER, fingerprint=fingerprint
        )
        if latest_run is not None and now - latest_run.generated_at <= timedelta(
            seconds=reuse_seconds
        ):
            return latest_run.id

    result = generate_recommendations(
        session, user_id, RecommendationMode.DISCOVER, settings, now, feed_filters=feed_filters
    )
    return result.run_id


@router.post(
    "/articles/{article_id}/recommendations",
    response_model=ArticleRecommendationsResponse,
    dependencies=[Depends(enforce_recommendation_rate_limit)],
    responses=RATE_LIMITED_RESPONSES,
)
def create_article_recommendations(
    article_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user_id: UserIdDep,
    now: NowDep,
) -> ArticleRecommendationsResponse:
    """指定記事に近い記事を推薦する（`PROJECT_SPEC.md` §13.1）。

    構成比は適用せず、`rank_candidates` の上位をそのまま rank 昇順で返す
    （`recommendation/service.py` の ARTICLE_BASED モード）。
    """
    source_article = session.get(Article, article_id)
    if source_article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="記事が見つかりません")

    result = generate_recommendations(
        session,
        user_id,
        RecommendationMode.ARTICLE_BASED,
        settings,
        now,
        source_article_id=article_id,
    )

    # 保存直後の run から、rank 昇順で items 件数ぶんだけ読み戻す。
    # DB 由来の値で組み立てることで、レスポンスと `recommendations.reasons` の
    # 内容が常に一致する（受入基準）。
    rows = (
        load_recommendation_page(session, result.run_id, limit=len(result.items))
        if result.items
        else ()
    )
    return ArticleRecommendationsResponse(
        run_id=result.run_id,
        mode=result.mode.value,
        generated_at=result.generated_at,
        items=_build_items(session, user_id, rows),
    )


@router.get(
    "/feed",
    response_model=FeedResponse,
    dependencies=[Depends(enforce_recommendation_rate_limit)],
    responses=RATE_LIMITED_RESPONSES,
)
def get_feed(
    session: SessionDep,
    settings: SettingsDep,
    user_id: UserIdDep,
    now: NowDep,
    q: Annotated[
        str | None,
        Query(
            description="検索語。title/translated_title/summary_jaへの部分一致（大文字小文字を区別しない）"
        ),
    ] = None,
    topics: Annotated[
        list[str] | None,
        Query(description="トピック。複数指定時は指定した全てを含む記事に絞る（AND）"),
    ] = None,
    technologies: Annotated[
        list[str] | None,
        Query(description="技術タグ。複数指定時は指定した全てを含む記事に絞る（AND）"),
    ] = None,
    published_from: Annotated[
        datetime | None, Query(description="公開日の下限（published_at、NULLはfetched_atで代替）")
    ] = None,
    published_to: Annotated[datetime | None, Query(description="公開日の上限")] = None,
    source_domain: Annotated[str | None, Query(description="情報源ドメインの完全一致")] = None,
    page: Annotated[int, Query(ge=1, description="1始まりのページ番号")] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> FeedResponse:
    """Discover フィードを返す（`PROJECT_SPEC.md` §13.2、検索・絞り込み・ページングは Issue #90）。

    `q` / `topics` / `technologies` / `published_from` / `published_to` /
    `source_domain` はいずれも省略可能な絞り込み条件で、全候補を対象に推薦を
    作り直す（決定済みの設計、`generate_recommendations` の `feed_filters`）。
    条件を反映したフィンガープリントが一致する直近の DISCOVER run が再利用して
    よい時間内（`config/scoring.yaml` の `limits.feed_run_reuse_seconds`）なら
    その run を再利用し、そうでなければ新しい run を生成する
    （`_resolve_discover_run_id`）。

    ページングは番号付き（`page` / `limit`）で、run 内の rank に対する offset
    として扱う。`total_count` は run に保存された件数（Bad 除外前）であり、
    範囲外の `page` はエラーにせず空の `items` を返す。

    古い run は `jobs/handlers/purge_recommendation_runs.py` が保持期間超過分を
    削除し、呼び出し過多は `rate_limit.py` のレート制限（Issue #28）が抑える。
    """
    feed_filters = FeedFilters(
        query=q,
        topics=tuple(topics) if topics else (),
        technologies=tuple(technologies) if technologies else (),
        published_from=published_from,
        published_to=published_to,
        source_domain=source_domain,
    )
    run_id = _resolve_discover_run_id(session, settings, user_id, now, feed_filters)

    total_count = count_recommendations(session, run_id)
    total_pages = math.ceil(total_count / limit) if total_count > 0 else 0
    offset = (page - 1) * limit
    rows = load_recommendation_page(session, run_id, offset=offset, limit=limit)

    return FeedResponse(
        items=_build_items(session, user_id, rows),
        total_count=total_count,
        page=page,
        page_size=limit,
        total_pages=total_pages,
    )
