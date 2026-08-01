"""重複排除の DB 反映（`PROJECT_SPEC.md` §17）。

`dedup.rules` の純粋関数（クラスタリング・代表選定・独自価値候補の絞り込み）で
導いた判定結果を `Article` へ書き戻す。判定そのものは持たず、ここでは
「DB のどの記事に何を反映するか」と「その根拠をどう記録するか」だけを担う。
"""

from __future__ import annotations

import logging
import re
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
    DedupLimits,
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

# ログ行を偽装しうる制御文字。改行・復帰・タブを含む C0 制御文字と DEL を
# 落とす。`dedup/judge.py` の `_CONTROL_CHARACTERS` は LLM が生成する複数行の
# 判定理由を保持するため改行を残すが、ここではログ 1 行の完全性を守ることが
# 目的のため改行・復帰も対象にする。
_LOG_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

# ログへ出す外部由来の値の長さ上限。`articles.source_type` / `content_type` は
# DB 上は自由文字列のため、長大な値や改行を含む値が紛れてもログ行を偽装したり
# ログを肥大化させたりしないよう制限する。
MAX_LOGGED_VALUE_LENGTH = 100


@dataclass(frozen=True)
class DeduplicationResult:
    """重複排除 1 回分の実行結果。"""

    processed_articles: int
    cluster_count: int
    duplicate_count: int
    # LLM を実際に呼んだ回数。
    llm_call_count: int
    # 独自価値判定のキャッシュを再利用し、LLM 呼び出しを省いた件数。
    llm_cache_hit_count: int
    # `DedupLimits.max_llm_calls_per_run` に到達し、以降の候補を判定せず
    # 安全側（重複）として扱った実行だったかどうか。
    llm_call_limit_reached: bool


def _sanitize_for_log(value: str) -> str:
    """ログへ出す外部由来の値から制御文字を除き、長さを制限する。

    `articles.source_type` / `content_type` は DB 上は自由文字列のため、
    改行文字が紛れるとログ行を偽装されるおそれがある。制御文字を除去した
    うえで長さも制限する。
    """
    cleaned = _LOG_CONTROL_CHARACTERS.sub("", value)
    return cleaned[:MAX_LOGGED_VALUE_LENGTH]


def _parse_source_type(value: str | None, *, article_id: uuid.UUID) -> SourceType:
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
            "重複判定に使えない source_type です。UNKNOWN へ読み替えます: article_id=%s value=%s",
            article_id,
            _sanitize_for_log(value),
        )
        return SourceType.UNKNOWN


def _parse_content_type(value: str | None, *, article_id: uuid.UUID) -> ContentType | None:
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
        logger.warning(
            "重複判定に使えない content_type です。None へ読み替えます: article_id=%s value=%s",
            article_id,
            _sanitize_for_log(value),
        )
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
        source_type=_parse_source_type(article.source_type, article_id=article.id),
        content_type=_parse_content_type(article.content_type, article_id=article.id),
        technical_quality=article.technical_quality,
        published_at=article.published_at,
    )


def _target_articles(
    session: Session, *, since: datetime, limits: DedupLimits
) -> Sequence[Article]:
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

    期間を絞ってもなお `limits.max_articles_per_run` を超える場合は、収集元が
    短期間に大量の記事を入れてきたケースを想定した安全弁として、決定的な順序
    （記事 ID 昇順）で上限まで切り捨てる。黙って切ると気付けないため、
    切り捨てた件数を warning ログに残す。記事 ID 昇順にするのは、
    クラスタリング自体の入力順を決定的にするため（`unique_value_candidates`
    の二次キーと同じく、SQL が保証しない行順に結果を依存させない）。
    """
    published_or_fetched_at = func.coalesce(Article.published_at, Article.fetched_at)
    filters = (Article.is_dead.is_(False), published_or_fetched_at >= since)

    total = session.scalar(select(func.count()).select_from(Article).where(*filters)) or 0

    stmt = select(Article).where(*filters).order_by(Article.id).limit(limits.max_articles_per_run)
    articles = session.scalars(stmt).all()

    if total > limits.max_articles_per_run:
        truncated_count = total - limits.max_articles_per_run
        logger.warning(
            "重複判定の対象記事数が上限を超えたため切り捨てました: "
            "total=%s limit=%s truncated_count=%s",
            total,
            limits.max_articles_per_run,
            truncated_count,
        )
    return articles


def _needs_unique_value_judgment(article: Article) -> bool:
    """独自価値判定を LLM へ問い直す必要があるかを判定する。

    `unique_value_judged_body_hash` が現在の `body_hash` と一致していれば、
    前回の判定結果（`has_unique_value`）をそのまま使い回せる
    （`analysis/service.py` の `needs_analysis` と同じキャッシュの考え方）。

    `body_hash` が None の記事は特別扱いする。`unique_value_judged_body_hash`
    は判定時点の `body_hash` をそのまま保存するだけなので、body_hash が None の
    記事を一度判定すると `unique_value_judged_body_hash` も None のまま保存
    される。その結果「一度も判定していない」のか「判定済みで本文も変わって
    いない」のかを、この 2 列だけでは区別できない
    （`needs_analysis` は `summary_ja` の有無でこの 2 つを区別しているが、
    本 MR では列を増やさない設計にしたため同じ手段が使えない）。
    区別できない以上、キャッシュを信頼すると「一度も判定していない記事を
    独自価値なしとみなす」誤りが起こりうるため、安全側に倒して
    body_hash が無い記事は常に判定し直す。
    """
    if article.body_hash is None:
        return True
    return article.unique_value_judged_body_hash != article.body_hash


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

    ただし冪等なのは「同一の対象記事集合」に対してのみである。ルックバック窓の
    外へ代表記事が出ると、窓内に残った非代表記事が単独クラスタとなり自身が
    代表へ戻る（カレンダー時間で窓がスライドする運用では成り立たない）。
    ただし推薦自体が 7 日フィルター（`PROJECT_SPEC.md` §15）を持ち窓外の記事は
    表示対象にならないため、窓内で見える記事の中から代表を選び直すのは
    意図した挙動である。
    """
    resolved_since = since or (datetime.now(UTC) - timedelta(days=lookback_days))
    config = get_dedup_config()
    thresholds = config.to_thresholds()
    penalties = config.to_penalties()
    unique_value_settings = config.to_unique_value_settings()
    limits = config.to_limits()

    articles = _target_articles(session, since=resolved_since, limits=limits)
    articles_by_id = {article.id: article for article in articles}
    signatures = tuple(_to_signature(article) for article in articles)
    clusters = cluster_articles(signatures, thresholds)

    duplicate_count = 0
    llm_call_count = 0
    llm_cache_hit_count = 0
    llm_call_limit_reached = False

    for cluster in clusters:
        representative = select_representative(cluster)
        candidates = unique_value_candidates(cluster, representative, unique_value_settings)
        candidate_ids = {candidate.id for candidate in candidates}

        unique_ids: set[uuid.UUID] = set()
        for candidate in candidates:
            candidate_article = articles_by_id[candidate.id]

            if not _needs_unique_value_judgment(candidate_article):
                # 前回判定時から本文が変わっていないため、判定結果を使い回す
                # （§24 コスト管理: 同一記事の再解析を避ける）。
                llm_cache_hit_count += 1
                has_unique_value = candidate_article.has_unique_value
            elif llm_call_count >= limits.max_llm_calls_per_run:
                # 呼び出し上限に達した以降は判定せず安全側（重複）として扱う。
                # `PROJECT_SPEC.md` §17 の原則（公式記事を優先する）と同じ理由で、
                # 判定できない記事は独自価値なしへ倒す。判定していないため
                # キャッシュ列は更新しない（次回実行で改めて判定を試みる）。
                llm_call_limit_reached = True
                has_unique_value = False
            else:
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
                candidate_article.unique_value_judged_body_hash = candidate_article.body_hash
                candidate_article.has_unique_value = has_unique_value

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

    if llm_call_limit_reached:
        logger.warning(
            "重複判定の LLM 呼び出し上限に到達したため、残りの候補は安全側"
            "（重複）として扱いました: limit=%s",
            limits.max_llm_calls_per_run,
        )

    session.flush()
    return DeduplicationResult(
        processed_articles=len(articles),
        cluster_count=len(clusters),
        duplicate_count=duplicate_count,
        llm_call_count=llm_call_count,
        llm_cache_hit_count=llm_cache_hit_count,
        llm_call_limit_reached=llm_call_limit_reached,
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
    （`llm.retry._record` と同じ方針）。ログの書き込みが失敗しても、
    `_apply_cluster` が既にセッションへ加えた `duplicate_of_article_id` /
    `duplicate_penalty` の変更は SAVEPOINT の外側にあるため失われず、
    `deduplicate_articles` 末尾の `session.flush()` でそのまま反映される。
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
