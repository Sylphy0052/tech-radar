"""重複排除の DB 反映（`PROJECT_SPEC.md` §17）。

`dedup.rules` の純粋関数（クラスタリング・代表選定・独自価値候補の絞り込み）で
導いた判定結果を `Article` へ書き戻す。判定そのものは持たず、ここでは
「DB のどの記事に何を反映するか」と「その根拠をどう記録するか」だけを担う。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from techradar.db import Article, OperationLog
from techradar.db.enums import ContentType, JobType, SourceType
from techradar.dedup.config import get_dedup_config
from techradar.dedup.judge import judge_unique_value
from techradar.dedup.rules import (
    ArticleCluster,
    ArticleSignature,
    DuplicateMatch,
    DuplicatePenalties,
    MatchMethod,
    cluster_articles,
    duplicate_penalty_for,
    select_representative,
    unique_value_candidates,
)
from techradar.llm import LLMProvider

logger = logging.getLogger(__name__)

OPERATION = JobType.DEDUPLICATE_ARTICLES.value

# 総当たり判定（`cluster_articles` は O(n^2)）を実用時間に収めるための対象期間。
# 推薦の 7 日フィルターと運用対象期間を合わせておけば、推薦対象になり得ない
# 古い記事まで比較する無駄も生まれない。
DEFAULT_LOOKBACK_DAYS = 7

# 一致の確度が高い順。`find_duplicate_match` が評価する順序と同じにし、
# 推移的に連結されただけで代表と直接の一致情報を持たない記事の減点根拠を、
# その記事が関わる一致の中から最も確度の高いものへ決定的に絞り込むために使う。
_METHOD_CONFIDENCE_RANK: dict[MatchMethod, int] = {
    MatchMethod.CANONICAL_URL: 0,
    MatchMethod.NORMALIZED_URL: 1,
    MatchMethod.BODY_HASH: 2,
    MatchMethod.TITLE: 3,
    MatchMethod.EMBEDDING: 4,
}


@dataclass(frozen=True)
class DeduplicationResult:
    """重複排除 1 回分の実行結果。"""

    processed_articles: int
    cluster_count: int
    duplicate_count: int
    llm_call_count: int


def _parse_source_type(value: str | None) -> SourceType:
    """DB の text 列を `SourceType` へ変換する。

    `source_type` は列挙外の値を弾かない text 列で保持している
    （マイグレーション不要で選択肢を増やせるようにするため）。未知の値で判定
    全体を止めないよう、不正な値は UNKNOWN（既定値）へ寄せて処理を続ける。
    """
    if value is None:
        return SourceType.UNKNOWN
    try:
        return SourceType(value)
    except ValueError:
        logger.warning(
            "重複判定に使えない source_type です。UNKNOWN へ読み替えます: value=%s", value
        )
        return SourceType.UNKNOWN


def _parse_content_type(value: str | None) -> ContentType | None:
    """DB の text 列を `ContentType` へ変換する。

    未解析でまだ値が入っていない記事は None（既定値）のままにする。
    `source_type` と異なり「未分類」を表す列挙値を持たないため、None を
    そのまま「不明」として扱う（`unique_value_candidates` の絞り込みで
    自然に対象外になる）。列挙外の値が紛れた場合も同様に None へ寄せる。
    """
    if value is None:
        return None
    try:
        return ContentType(value)
    except ValueError:
        logger.warning("重複判定に使えない content_type です。None へ読み替えます: value=%s", value)
        return None


def _to_signature(article: Article) -> ArticleSignature:
    """`Article` を判定用の `ArticleSignature` へ変換する。"""
    return ArticleSignature(
        id=article.id,
        canonical_url=article.canonical_url,
        original_url=article.original_url,
        title=article.title,
        body_hash=article.body_hash,
        embedding=tuple(article.embedding) if article.embedding is not None else None,
        source_authority=article.source_authority,
        source_type=_parse_source_type(article.source_type),
        content_type=_parse_content_type(article.content_type),
        technical_quality=article.technical_quality,
        published_at=article.published_at,
    )


def _target_articles(session: Session, *, since: datetime) -> Sequence[Article]:
    """重複判定の対象記事を絞り込む。

    `cluster_articles` は総当たり O(n^2) のため、全記事を渡すと記事数の増加に
    伴って実行時間が二次的に伸び実用にならない。生きている記事
    （`is_dead` が False）かつ `since` 以降の記事だけに絞る。
    重複は「最近入ってきた記事同士が同じニュースを指しているか」を見る用途が
    主であり、対象期間を絞っても実務上のクラスタ検出漏れにはならない。

    期間の基準は `published_at` を優先するが、RSS/HTML から公開日を取得
    できなかった記事（まとめ・転載系サイトに多い）は `published_at` が
    NULL になる。まさに重複判定したい記事が対象から丸ごと漏れてしまうため、
    NULL の場合は `fetched_at`（NOT NULL）で代替する。
    """
    published_or_fetched_at = func.coalesce(Article.published_at, Article.fetched_at)
    stmt = select(Article).where(Article.is_dead.is_(False), published_or_fetched_at >= since)
    return session.scalars(stmt).all()


def _best_match_for(member_id: uuid.UUID, cluster: ArticleCluster) -> DuplicateMatch:
    """クラスタ内で `member_id` が関わる一致のうち、最も確度の高いものを返す。

    推移的に連結された記事は代表と直接のペアを持たないことがある
    （a-b, b-c と連結され代表が a の場合、c は a と直接比較されていない）。
    その場合も減点の根拠を一意に決めるため、この記事が関わる全ペアから
    `MatchMethod` の判定順（確実な URL 系 → 曖昧な Embedding 系）で
    最も確度の高いものを選ぶ。クラスタは 2 件以上あれば必ず union-find の
    根拠となった一致を持つため、対象記事が非代表であれば必ず 1 件以上見つかる。
    """
    incident = [
        match for left_id, right_id, match in cluster.matches if member_id in (left_id, right_id)
    ]
    if not incident:
        message = f"クラスタ内に一致情報がありません: member_id={member_id}"
        raise ValueError(message)
    return min(incident, key=lambda match: _METHOD_CONFIDENCE_RANK[match.method])


def deduplicate_articles(
    session: Session,
    provider: LLMProvider,
    *,
    since: datetime | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    job_id: uuid.UUID | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DeduplicationResult:
    """直近記事の重複を判定し、`Article` へ反映する。

    冪等に動く。クラスタ内の全記事について `duplicate_of_article_id` /
    `duplicate_penalty` を毎回明示的に設定し直すため、再実行しても前回の
    判定が残らない。
    """
    resolved_since = since or (datetime.now(UTC) - timedelta(days=lookback_days))
    config = get_dedup_config()
    thresholds = config.to_thresholds()
    penalties = config.to_penalties()
    unique_value_settings = config.to_unique_value_settings()

    articles = _target_articles(session, since=resolved_since)
    articles_by_id = {article.id: article for article in articles}
    signatures = tuple(_to_signature(article) for article in articles)
    clusters = cluster_articles(signatures, thresholds)

    duplicate_count = 0
    llm_call_count = 0

    for cluster in clusters:
        representative = select_representative(cluster)
        candidates = unique_value_candidates(cluster, representative, unique_value_settings)
        candidate_ids = {candidate.id for candidate in candidates}

        unique_ids: set[uuid.UUID] = set()
        for candidate in candidates:
            candidate_article = articles_by_id[candidate.id]
            has_unique_value = judge_unique_value(
                provider,
                title=candidate_article.title,
                body=candidate_article.body or "",
                job_id=job_id,
                session=session,
                article_id=candidate_article.id,
                sleep=sleep,
            )
            llm_call_count += 1
            if has_unique_value:
                unique_ids.add(candidate.id)

        match_by_member_id = {
            member.id: _best_match_for(member.id, cluster)
            for member in cluster.members
            if member.id != representative.id
        }
        duplicate_count += _apply_cluster(
            cluster,
            representative=representative,
            articles_by_id=articles_by_id,
            unique_ids=unique_ids,
            match_by_member_id=match_by_member_id,
            penalties=penalties,
        )
        _log_cluster_decision(
            session,
            job_id=job_id,
            cluster=cluster,
            representative=representative,
            candidate_ids=candidate_ids,
            unique_ids=unique_ids,
            match_by_member_id=match_by_member_id,
        )

    session.flush()
    return DeduplicationResult(
        processed_articles=len(articles),
        cluster_count=len(clusters),
        duplicate_count=duplicate_count,
        llm_call_count=llm_call_count,
    )


def _apply_cluster(
    cluster: ArticleCluster,
    *,
    representative: ArticleSignature,
    articles_by_id: dict[uuid.UUID, Article],
    unique_ids: set[uuid.UUID],
    match_by_member_id: dict[uuid.UUID, DuplicateMatch],
    penalties: DuplicatePenalties,
) -> int:
    """クラスタ 1 件分の判定を `Article` へ反映し、重複と判定した件数を返す。"""
    duplicate_count = 0
    for member in cluster.members:
        article = articles_by_id[member.id]
        if member.id == representative.id or member.id in unique_ids:
            # 代表自身、および LLM が独自価値ありと判定した記事は別記事として残す。
            # 再実行のたびに前回の判定を打ち消すため、常に明示的に初期状態へ戻す。
            article.duplicate_of_article_id = None
            article.duplicate_penalty = 0.0
            continue

        match = match_by_member_id[member.id]
        article.duplicate_of_article_id = representative.id
        article.duplicate_penalty = duplicate_penalty_for(match, penalties)
        duplicate_count += 1
    return duplicate_count


def _log_cluster_decision(
    session: Session,
    *,
    job_id: uuid.UUID | None,
    cluster: ArticleCluster,
    representative: ArticleSignature,
    candidate_ids: set[uuid.UUID],
    unique_ids: set[uuid.UUID],
    match_by_member_id: dict[uuid.UUID, DuplicateMatch],
) -> None:
    """クラスタごとの判定根拠を `operation_logs` へ残す（§24 可観測性）。

    どの記事がどの段で重複と判定され、独自価値判定に回った候補がどう
    判定されたかを後から追えるようにする。単独記事のクラスタは判定材料が
    無いため記録しない。

    ログ書き込みの失敗で本処理を止めないよう SAVEPOINT に閉じ込める
    （`llm.retry._record` と同じ方針）。
    """
    if len(cluster.members) <= 1:
        return

    members_detail = [
        _member_detail(member, representative, candidate_ids, unique_ids, match_by_member_id)
        for member in cluster.members
    ]
    try:
        with session.begin_nested():
            session.add(
                OperationLog(
                    operation=OPERATION,
                    status="completed",
                    job_id=job_id,
                    details={
                        "representative_article_id": str(representative.id),
                        "member_count": len(cluster.members),
                        "members": members_detail,
                    },
                )
            )
    except SQLAlchemyError:
        logger.warning("operation_logs への重複判定記録に失敗しました", exc_info=True)


def _member_detail(
    member: ArticleSignature,
    representative: ArticleSignature,
    candidate_ids: set[uuid.UUID],
    unique_ids: set[uuid.UUID],
    match_by_member_id: dict[uuid.UUID, DuplicateMatch],
) -> dict[str, object]:
    """1 記事分のログ詳細を組み立てる。"""
    is_representative = member.id == representative.id
    is_candidate = member.id in candidate_ids
    match = match_by_member_id.get(member.id)
    return {
        "article_id": str(member.id),
        "is_representative": is_representative,
        "is_unique_value_candidate": is_candidate,
        "has_unique_value": (member.id in unique_ids) if is_candidate else None,
        "match_method": match.method.value if match is not None else None,
        "match_similarity": match.similarity if match is not None else None,
    }
