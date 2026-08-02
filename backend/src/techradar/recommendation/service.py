"""推薦の DB 連携と永続化（`PROJECT_SPEC.md` §6.1, §7.1, §13, §14, §15, §19）。

`ranking.py` / `composition.py` の純粋関数（採点・構成比の適用）へ委譲し、
ここでは「DB のどのデータから採点対象を組み立てるか」と「結果をどう保存するか」
だけを担う（`dedup/service.py` と同じ責務分割）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db import (
    Article,
    ArticleFeedback,
    Recommendation,
    RecommendationRun,
    SourceRegistry,
    UserArticle,
)
from techradar.db.enums import ArticleOrigin, FeedbackAction, RecommendationMode
from techradar.interest.service import (
    load_weighted_interest_articles,
    order_by_recency_and_truncate,
)
from techradar.recommendation.composition import CompositionStats, compose_feed_with_stats
from techradar.recommendation.config import get_scoring_config
from techradar.recommendation.ranking import (
    CandidateSignature,
    InterestProfile,
    ScoredCandidate,
    WeightedEmbedding,
    rank_candidates,
)

logger = logging.getLogger(__name__)

# 経過日数を求めるための秒数（`ranking.py` の同名の私有定数と同じ値、モジュールを
# またいで private 定数を共有しないためここでも定義する）。
_SECONDS_PER_DAY = 86400

# 記事起点推薦（ARTICLE_BASED）における起点記事の重み。時間減衰やフィードバック
# 強度といったユーザー関心プロファイル特有の重み付けは意味を持たないため固定値
# にする（`_build_article_based_profile` のdocstring参照）。
_SOURCE_ARTICLE_WEIGHT = 1.0

# 既に自分のものになっている（Discover へ再掲する価値が無い）とみなす origin。
_OWNED_ORIGIN_VALUES = frozenset(
    origin.value for origin in (ArticleOrigin.MANUAL, ArticleOrigin.GOOD, ArticleOrigin.SAVED)
)
# 既読とみなす origin（`PROJECT_SPEC.md` §6.1「既読記事の再表示は抑制する」）。
# `api/recommendations.py`（レスポンスの is_read 判定）と共有するため公開名にする。
READ_ORIGIN_VALUES = frozenset(
    origin.value for origin in (ArticleOrigin.READ_FULL, ArticleOrigin.CLICKED)
)


@dataclass(frozen=True)
class RecommendationResult:
    """`generate_recommendations` の戻り値。"""

    run_id: uuid.UUID
    mode: RecommendationMode
    generated_at: datetime
    items: tuple[ScoredCandidate, ...]
    # Discover のときだけ構成比適用の統計を持つ（記事起点推薦は構成比を適用しない）。
    composition_stats: CompositionStats | None = None


def _load_bad_embeddings(
    session: Session, user_id: uuid.UUID, limit: int
) -> tuple[tuple[float, ...], ...]:
    """ユーザーが Bad にした記事の embedding 群を新しい順に集める。

    Bad 近傍抑制（`ranking.compute_bad_similarity_penalty`）の入力になる。
    `build_interest_profile`（Discover）・`_build_article_based_profile`
    （記事起点推薦）の両方から使う共通処理。
    """
    bad_feedback_rows = session.execute(
        select(ArticleFeedback.article_id, ArticleFeedback.created_at).where(
            ArticleFeedback.user_id == user_id,
            ArticleFeedback.action == FeedbackAction.BAD.value,
        )
    ).all()
    created_at_by_article_id: dict[uuid.UUID, datetime] = {}
    for article_id, created_at in bad_feedback_rows:
        created_at_by_article_id[article_id] = created_at
    target_article_ids = order_by_recency_and_truncate(
        created_at_by_article_id, limit, "Bad プロファイル構築対象"
    )
    if not target_article_ids:
        return ()

    bad_articles = session.scalars(select(Article).where(Article.id.in_(target_article_ids))).all()
    return tuple(
        tuple(article.embedding) for article in bad_articles if article.embedding is not None
    )


def build_interest_profile(
    session: Session, user_id: uuid.UUID, now: datetime, settings: Settings
) -> InterestProfile:
    """ユーザーの関心プロファイルを構築する（`PROJECT_SPEC.md` §8）。

    対象記事の抽出・重み計算は `interest.service.load_weighted_interest_articles`
    に委ねる（`rebuild_interest_clusters` と同じ対象・同じ重み計算を共有する
    ための共通化、DRY）。ここでは Discover の関心プロファイル
    （`InterestProfile`）へ組み立て直すことだけを行う。`settings` は他の DB
    連携関数と揃えた引数だが、この関数は現時点では `Settings` の値を参照しない
    （将来 Settings 側にプロファイル関連の項目が増えた場合に備えて残す）。

    重み計算の詳細（`effective_interest` の各項の採用値）は
    `load_weighted_interest_articles` の docstring を参照。

    Bad 済み記事（`article_feedback.action='bad'`）の embedding は
    `InterestProfile.bad_embeddings` へ別途集める（`_load_bad_embeddings`）。
    件数上限は `config/scoring.yaml` の `interest.max_bad_profile_articles`
    で管理し、新しい順に打ち切る。
    """
    del settings  # 現時点では未使用（呼び出し側との引数統一のために残す）。

    weighted_articles = load_weighted_interest_articles(session, user_id, now)

    weighted_embeddings: list[WeightedEmbedding] = []
    known_topics: set[str] = set()
    for record in weighted_articles:
        known_topics.update(record.topics)
        if record.embedding is None:
            continue
        weighted_embeddings.append(WeightedEmbedding(vector=record.embedding, weight=record.weight))

    config = get_scoring_config()
    bad_embeddings = _load_bad_embeddings(
        session, user_id, config.interest.max_bad_profile_articles
    )

    return InterestProfile(
        embeddings=tuple(weighted_embeddings),
        known_topics=frozenset(known_topics),
        bad_embeddings=bad_embeddings,
    )


def load_candidates(
    session: Session,
    user_id: uuid.UUID,
    now: datetime,
    settings: Settings,
    *,
    source_article_id: uuid.UUID | None = None,
) -> tuple[CandidateSignature, ...]:
    """推薦候補記事を読み込む（`PROJECT_SPEC.md` §6.1, §6.2, §7.2）。

    以下を除外する。

    * リンク切れ記事（`is_dead`）
    * 公開から `config/scoring.yaml` の `freshness.max_age_days` を超えた記事
      （`published_at` が NULL なら `fetched_at` で代替する）。freshness スコアの
      減衰基準（`ranking.compute_freshness`）と単一の真実源を共有する
    * この user が Bad 済みの記事
    * この user が既に関心記事として登録済み（origin が manual/good/saved）の記事
    * `source_article_id`（記事起点推薦の起点記事自身）

    Bad 済み・登録済みの除外は、ユーザーの履歴が伸びるほど大きくなる ID 集合を
    `NOT IN` に渡すのではなく、`article_feedback` / `user_articles` への相関
    サブクエリ（`NOT EXISTS`）で行う。バインドパラメータ数が履歴サイズに比例して
    増え続けるのを避けるため。

    `user_articles`（is_read 判定用）と `source_registry` は候補記事数に
    関わらず 1 回ずつ取得して辞書化し、N+1 クエリを避ける。

    `settings` は他の DB 連携関数と揃えた引数だが、この関数は現時点では
    `Settings` の値を参照しない（`recommendation_max_age_days` は Issue #11 の
    自己レビューで `scoring.yaml` の `freshness.max_age_days` に一本化し削除した）。
    """
    del settings  # 現時点では未使用（呼び出し側との引数統一のために残す）。

    config = get_scoring_config()
    max_candidates = config.limits.max_candidates_per_run
    since = now - timedelta(days=config.freshness.max_age_days)

    origins_by_article_id: dict[uuid.UUID, set[str]] = {}
    for article_id, origin in session.execute(
        select(UserArticle.article_id, UserArticle.origin).where(UserArticle.user_id == user_id)
    ).all():
        origins_by_article_id.setdefault(article_id, set()).add(origin)

    bad_exists = (
        select(ArticleFeedback.article_id)
        .where(
            ArticleFeedback.article_id == Article.id,
            ArticleFeedback.user_id == user_id,
            ArticleFeedback.action == FeedbackAction.BAD.value,
        )
        .correlate(Article)
        .exists()
    )
    owned_exists = (
        select(UserArticle.article_id)
        .where(
            UserArticle.article_id == Article.id,
            UserArticle.user_id == user_id,
            UserArticle.origin.in_(_OWNED_ORIGIN_VALUES),
        )
        .correlate(Article)
        .exists()
    )

    published_or_fetched_at = func.coalesce(Article.published_at, Article.fetched_at)
    filters = [
        Article.is_dead.is_(False),
        published_or_fetched_at >= since,
        ~bad_exists,
        ~owned_exists,
    ]
    if source_article_id is not None:
        filters.append(Article.id != source_article_id)

    total = session.scalar(select(func.count()).select_from(Article).where(*filters)) or 0
    articles = session.scalars(
        select(Article)
        .where(*filters)
        .order_by(Article.published_at.desc().nulls_last(), Article.id.asc())
        .limit(max_candidates)
    ).all()

    if total > max_candidates:
        truncated_count = total - max_candidates
        logger.warning(
            "推薦候補の対象記事数が上限を超えたため切り捨てました: "
            "total=%s limit=%s truncated_count=%s",
            total,
            max_candidates,
            truncated_count,
        )

    if not articles:
        return ()

    domains = {article.source_domain for article in articles}
    entity_names_by_domain: dict[str, list[str]] = {}
    for domain, entity_name, github_org in session.execute(
        select(SourceRegistry.domain, SourceRegistry.entity_name, SourceRegistry.github_org).where(
            SourceRegistry.domain.in_(domains)
        )
    ).all():
        names = entity_names_by_domain.setdefault(domain, [])
        names.append(entity_name)
        if github_org is not None:
            names.append(github_org)

    return tuple(
        _to_candidate_signature(
            article,
            is_read=bool(origins_by_article_id.get(article.id, set()) & READ_ORIGIN_VALUES),
            source_entity_names=tuple(entity_names_by_domain.get(article.source_domain, ())),
        )
        for article in articles
    )


def _to_candidate_signature(
    article: Article, *, is_read: bool, source_entity_names: tuple[str, ...]
) -> CandidateSignature:
    """`Article` を採点用の `CandidateSignature` へ変換する。

    `is_bad` は `load_candidates` の時点で既に除外済みのため常に False にする
    （純粋関数側の仕様としてフィールド自体は残す）。
    """
    return CandidateSignature(
        id=article.id,
        embedding=tuple(article.embedding) if article.embedding is not None else None,
        source_authority=article.source_authority,
        is_primary_source=article.is_primary_source,
        source_domain=article.source_domain,
        source_entity_names=source_entity_names,
        topics=tuple(article.topics),
        technologies=tuple(article.technologies),
        technical_quality=article.technical_quality,
        published_at=article.published_at,
        fetched_at=article.fetched_at,
        duplicate_penalty=article.duplicate_penalty,
        is_bad=False,
        is_read=is_read,
    )


def _build_article_based_profile(
    session: Session, user_id: uuid.UUID, source_article_id: uuid.UUID
) -> InterestProfile:
    """起点記事から記事起点推薦用の関心プロファイルを作る（`PROJECT_SPEC.md` §13.1）。

    起点記事に embedding が無ければ空プロファイルのまま返す。
    `compute_interest_similarity` は候補に embedding が無い、またはプロファイルが
    空なら 0.0 を返す仕様のため、これは例外にはならず「一致度で差が付かない」
    挙動になる。

    起点記事の重み（`WeightedEmbedding.weight`）は `_SOURCE_ARTICLE_WEIGHT`
    （1.0）に固定する。記事起点推薦は「この記事に近い記事」を探す用途で、
    起点は単一記事しか無く比較対象の候補が近いかどうかだけが問題になるため、
    時間減衰やフィードバック強度といったユーザーの関心プロファイル全体を
    要約するための重み付けはそもそも意味を持たない。

    `bad_embeddings` は記事起点推薦でも取得し、Bad 近傍抑制を効かせる。
    起点記事を経由した推薦であっても、ユーザーが明示的に Bad と判断した
    記事に近い候補を勧めるべきではないのは Discover と共通の要件のため。

    記事起点推薦では `known_topics` が起点記事自身の topics になるため、
    `compute_novelty`（新規性）は「起点記事とどれだけトピックを共有していないか」
    の裏返しとして働く。つまりここでの novelty は一般的な「ユーザーにとって
    未知のテーマか」ではなく「起点記事と主題がどれだけ離れているか」を表す。
    """
    source_article = session.get(Article, source_article_id)
    if source_article is None:
        message = f"起点記事が見つかりません: {source_article_id}"
        raise ValueError(message)

    embeddings = (
        (WeightedEmbedding(vector=tuple(source_article.embedding), weight=_SOURCE_ARTICLE_WEIGHT),)
        if source_article.embedding is not None
        else ()
    )
    config = get_scoring_config()
    bad_embeddings = _load_bad_embeddings(
        session, user_id, config.interest.max_bad_profile_articles
    )
    return InterestProfile(
        embeddings=embeddings,
        known_topics=frozenset(source_article.topics),
        bad_embeddings=bad_embeddings,
    )


def generate_recommendations(
    session: Session,
    user_id: uuid.UUID,
    mode: RecommendationMode,
    settings: Settings,
    now: datetime,
    *,
    source_article_id: uuid.UUID | None = None,
) -> RecommendationResult:
    """推薦を生成し `recommendation_runs` / `recommendations` へ保存する。

    `PROJECT_SPEC.md` §13 の 2 モードに対応する。

    * DISCOVER: `build_interest_profile` の関心プロファイルを使い、構成比
      （`compose_feed_with_stats`）を適用したうえで `limits.feed_run_size` 件を保存する。
    * ARTICLE_BASED: 起点記事の embedding / topics から作った関心プロファイルを使い、
      構成比は適用せず `rank_candidates` の上位 `limits.article_based_run_size` 件を
      そのまま保存する。

    `commit` はしない。呼び出し側の `session_scope` に委ねる（`dedup/service.py` と
    同じ方針）。
    """
    config = get_scoring_config()
    scoring_settings = config.to_settings()

    if mode is RecommendationMode.DISCOVER:
        profile = build_interest_profile(session, user_id, now, settings)
        candidates = load_candidates(session, user_id, now, settings)
        scored = rank_candidates(candidates, profile, scoring_settings, now)
        composed = compose_feed_with_stats(scored, scoring_settings, config.limits.feed_run_size)
        items = composed.candidates
        composition_stats: CompositionStats | None = composed.stats
        run_source_article_id = None
    elif mode is RecommendationMode.ARTICLE_BASED:
        if source_article_id is None:
            message = "article_based モードには source_article_id が必要です"
            raise ValueError(message)
        profile = _build_article_based_profile(session, user_id, source_article_id)
        candidates = load_candidates(
            session, user_id, now, settings, source_article_id=source_article_id
        )
        scored = rank_candidates(candidates, profile, scoring_settings, now)
        items = scored[: config.limits.article_based_run_size]
        composition_stats = None
        run_source_article_id = source_article_id
    else:
        message = f"未対応の推薦モードです: {mode}"
        raise ValueError(message)

    run = RecommendationRun(
        user_id=user_id,
        source_article_id=run_source_article_id,
        mode=mode.value,
        generated_at=now,
    )
    session.add(run)
    session.flush()

    for rank, scored_candidate in enumerate(items, start=1):
        session.add(
            Recommendation(
                run_id=run.id,
                article_id=scored_candidate.candidate.id,
                score=scored_candidate.breakdown.total,
                reasons=scored_candidate.breakdown.to_reasons(),
                rank=rank,
            )
        )
    session.flush()

    return RecommendationResult(
        run_id=run.id,
        mode=mode,
        generated_at=now,
        items=items,
        composition_stats=composition_stats,
    )


def load_recommendation_page(
    session: Session,
    run_id: uuid.UUID,
    *,
    after_rank: int | None = None,
    limit: int,
) -> tuple[tuple[Recommendation, Article], ...]:
    """保存済み推薦を `rank` 昇順で読み出す（API のページングで使う）。

    `after_rank` を指定すると、それより大きい rank だけを返す。
    """
    if limit <= 0:
        message = f"limit は 1 以上にしてください: {limit}"
        raise ValueError(message)

    filters = [Recommendation.run_id == run_id]
    if after_rank is not None:
        filters.append(Recommendation.rank > after_rank)

    rows = session.execute(
        select(Recommendation, Article)
        .join(Article, Article.id == Recommendation.article_id)
        .where(*filters)
        .order_by(Recommendation.rank.asc())
        .limit(limit)
    ).all()
    return tuple((recommendation, article) for recommendation, article in rows)


def build_latest_run_select(
    user_id: uuid.UUID, mode: RecommendationMode
) -> Select[tuple[RecommendationRun]]:
    """最新 run を1件取る SELECT 文を組み立てる。

    `find_latest_run` から分離しているのは、実行計画を検証するテストが実際に
    発行される文と同じものを見られるようにするため（Issue #32）。
    """
    return (
        select(RecommendationRun)
        .where(RecommendationRun.user_id == user_id, RecommendationRun.mode == mode.value)
        .order_by(RecommendationRun.generated_at.desc(), RecommendationRun.id.desc())
        .limit(1)
    )


def find_latest_run(
    session: Session, user_id: uuid.UUID, mode: RecommendationMode
) -> RecommendationRun | None:
    """その user の最新 run（`generated_at` 降順、同値は `id` 降順）を返す。"""
    return session.scalars(build_latest_run_select(user_id, mode)).first()
