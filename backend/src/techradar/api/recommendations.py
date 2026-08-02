"""記事起点推薦と Discover フィードの API（`PROJECT_SPEC.md` §6.1, §13, §20）。

推薦の生成・保存自体は `recommendation/service.py`（T1〜T3）に委ね、ここでは
リクエストの受け口・レスポンス整形・cursor ページングだけを担う。
"""

from __future__ import annotations

import base64
import binascii
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
from techradar.db import Article, ArticleFeedback, Recommendation, RecommendationRun, UserArticle
from techradar.db.enums import FeedbackAction, RecommendationMode
from techradar.recommendation.config import get_scoring_config
from techradar.recommendation.service import (
    READ_ORIGIN_VALUES,
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

# cursor 文字列（デコード後）中の run_id と rank の区切り文字。
_CURSOR_SEPARATOR = ":"

# cursor がデコード後に含む rank 部分の桁数上限。実際の rank は
# limits.feed_run_size（config/scoring.yaml）以下に収まるため十分な余裕を
# 持たせつつ、極端に長い数字列を int() へ渡さないための安全弁。
_MAX_CURSOR_RANK_DIGITS = 10

# cursor（デコード前の raw 文字列: UUID 36 文字 + 区切り 1 文字 + rank 桁数上限）
# の最大長。
_MAX_CURSOR_RAW_LENGTH = 36 + len(_CURSOR_SEPARATOR) + _MAX_CURSOR_RANK_DIGITS
# base64（パディング無し）へエンコードした cursor 文字列の最大長。3 バイトごとに
# 4 文字になるため切り上げで計算する。FastAPI の `Query(max_length=...)` は超過時
# に 422 を返すが、他の壊れた cursor と同じ 400 に統一したいため、上限チェックは
# `_decode_cursor` 内で行い、ここでは定数としてのみ持つ。
CURSOR_MAX_LENGTH = math.ceil(_MAX_CURSOR_RAW_LENGTH / 3) * 4


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
    """Discover フィード（`GET /api/feed`）のレスポンス。"""

    items: list[RecommendationItem]
    # 次ページが無ければ null。
    next_cursor: str | None


class InvalidCursorError(Exception):
    """壊れた cursor 文字列を検出したときの例外。"""


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


def _encode_cursor(run_id: uuid.UUID, rank: int) -> str:
    """run_id と直前ページ最後の rank から、不透明な cursor 文字列を作る。

    URL セーフな base64 にし、末尾の `=` パディングは取り除く（クエリ文字列に
    そのまま載せやすくするため）。
    """
    raw = f"{run_id}{_CURSOR_SEPARATOR}{rank}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[uuid.UUID, int]:
    """cursor 文字列を run_id と rank へ復元する。

    壊れた cursor（長すぎる・base64 として不正・区切り文字が無い・rank の桁数が
    異常・UUID/整数として不正）はすべて `InvalidCursorError` にまとめる。
    呼び出し側で 400 に変換する。
    """
    if len(cursor) > CURSOR_MAX_LENGTH:
        raise InvalidCursorError

    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        run_id_text, rank_text = raw.rsplit(_CURSOR_SEPARATOR, 1)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidCursorError from exc

    # 極端に長い桁数の文字列を int() へ渡さないよう、変換前に桁数を検証する。
    # `CURSOR_MAX_LENGTH` はこの桁数から導出しているため、桁数超過の大半は
    # 手前の長さ検証で弾かれる。ここで実際に弾けるのは base64 の端数ぶんだけ
    # 長さ上限に収まってしまうケースで、独立した防御層ではない。
    if len(rank_text.removeprefix("-")) > _MAX_CURSOR_RANK_DIGITS:
        raise InvalidCursorError

    try:
        return uuid.UUID(run_id_text), int(rank_text)
    except ValueError as exc:
        raise InvalidCursorError from exc


def _resolve_discover_run_id(
    session: Session, settings: Settings, user_id: uuid.UUID, now: datetime
) -> uuid.UUID:
    """cursor 無しの `GET /api/feed` が使う run の id を決める。

    直近の DISCOVER run が再利用してよい時間内（`config/scoring.yaml` の
    `limits.feed_run_reuse_seconds`）なら新規生成せずその run を再利用し、
    そうでなければ新規生成する。`feed_run_reuse_seconds` が 0 の場合は常に
    新規生成する（無効化）。

    直近 run の読み取りと生成の間に排他制御は掛けないため、ほぼ同時に届いた
    cursor 無しのリクエストは、いずれも「再利用できる run が無い」と判断して
    それぞれ run を作りうる。単一ユーザー・ローカル実行の前提では実害が小さい
    ため許容する。古い run は `jobs/handlers/purge_recommendation_runs.py` が
    保持期間超過分を削除し、この関数自体の呼び出し過多は `rate_limit.py` の
    レート制限（Issue #28）が抑える。
    """
    reuse_seconds = get_scoring_config().limits.feed_run_reuse_seconds
    if reuse_seconds > 0:
        latest_run = find_latest_run(session, user_id, RecommendationMode.DISCOVER)
        if latest_run is not None and now - latest_run.generated_at <= timedelta(
            seconds=reuse_seconds
        ):
            return latest_run.id

    result = generate_recommendations(session, user_id, RecommendationMode.DISCOVER, settings, now)
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
    cursor: Annotated[
        str | None, Query(description="前回レスポンスの next_cursor をそのまま渡す")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> FeedResponse:
    """Discover フィードを返す（`PROJECT_SPEC.md` §13.2）。

    `cursor` 省略時は、直近の run が再利用してよい時間内（`config/scoring.yaml` の
    `limits.feed_run_reuse_seconds`）ならその run の先頭ページを返し、そうでなければ
    新しい run を生成して DISCOVER モードの先頭ページを返す（`_resolve_discover_run_id`）。
    `cursor` 指定時は cursor が指すのと同じ run を rank 順に辿ることで、
    ページ間で重複が出ないようにする（受入基準）。

    `next_cursor` は Bad 除外（`_build_items`）より前の行から計算する。除外の有無で
    cursor が巻き戻らないようにするためで、その結果 `items` が空でも `next_cursor` が
    非 null になりうる（ページ内が全件 Bad の場合）。呼び出し側は `items` の空だけで
    終端と判断せず、`next_cursor` が null になるまで辿ること。

    古い run は `jobs/handlers/purge_recommendation_runs.py` が保持期間超過分を
    削除し、呼び出し過多は `rate_limit.py` のレート制限（Issue #28）が抑える。
    """
    if cursor is None:
        run_id = _resolve_discover_run_id(session, settings, user_id, now)
        after_rank: int | None = None
    else:
        try:
            run_id, after_rank = _decode_cursor(cursor)
        except InvalidCursorError as exc:
            logger.warning("cursor のデコードに失敗しました")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="cursor が不正です"
            ) from exc

        run = session.get(RecommendationRun, run_id)
        # 別ユーザーの run・記事起点推薦の run（mode が違う）を指す cursor も、
        # このフィードの続きとしては無効として扱う。
        if run is None or run.user_id != user_id or run.mode != RecommendationMode.DISCOVER.value:
            logger.warning("cursor が指す run が見つからないか一致しません: run_id=%s", run_id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cursor が指す推薦結果が見つかりません",
            )

    # 次ページの有無を判定するため、要求件数より 1 件多く取得する。
    rows = load_recommendation_page(session, run_id, after_rank=after_rank, limit=limit + 1)
    has_next_page = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor = _encode_cursor(run_id, page_rows[-1][0].rank) if has_next_page else None
    return FeedResponse(
        items=_build_items(session, user_id, page_rows),
        next_cursor=next_cursor,
    )
