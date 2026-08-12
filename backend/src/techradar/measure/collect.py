"""3 項目の計測を DB から集める（Issue #74）。

各集計そのものは `body_length` / `clusters` / `feed_slots` が持つ。ここは DB から必要な
入力を読み、既存の実装（クラスタ構築・採点・構成比適用）を呼び出して結果へ橋渡しする。

推薦の保存は行わない。`recommendation.service.generate_recommendations` は
`recommendation_runs` / `recommendations` へ書き込むため、計測からは呼ばずに同じ順序で
組み立て直す（読み取り専用トランザクションでは書き込みが拒否されるため、呼べば失敗する）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from techradar.analysis.service import MAX_ANALYSIS_BODY_CHARACTERS
from techradar.config import Settings
from techradar.interest.clusters import ClusteringSettings, ClusterSource, build_interest_clusters
from techradar.interest.service import load_weighted_interest_articles
from techradar.measure.body_length import load_body_lengths, summarize_body_lengths
from techradar.measure.clusters import summarize_clusters
from techradar.measure.feed_slots import summarize_feed_slots
from techradar.measure.report import Measurements
from techradar.recommendation.composition import compose_feed_with_stats
from techradar.recommendation.config import get_scoring_config
from techradar.recommendation.ranking import rank_candidates
from techradar.recommendation.service import build_interest_profile, load_candidates


def _cluster_sources(
    session: Session, user_id: uuid.UUID, now: datetime
) -> tuple[ClusterSource, ...]:
    """クラスタリング対象を読む。

    `interest.service.rebuild_interest_clusters` と同じ対象・同じ重み計算を使う。
    計測用に別の抽出を書くと、測っている対象が本番のクラスタと食い違う。
    """
    weighted_articles = load_weighted_interest_articles(session, user_id, now)
    return tuple(
        ClusterSource(embedding=record.embedding, topics=record.topics, weight=record.weight)
        for record in weighted_articles
        if record.embedding is not None
    )


def collect_measurements(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    user_id: uuid.UUID | None = None,
) -> Measurements:
    """3 項目の計測結果を集める。データが無くても例外にしない。"""
    target_user_id = user_id if user_id is not None else settings.default_user_id
    config = get_scoring_config()
    scoring_settings = config.to_settings()

    body_length = summarize_body_lengths(
        load_body_lengths(session), limit=MAX_ANALYSIS_BODY_CHARACTERS
    )

    sources = _cluster_sources(session, target_user_id, now)
    clustering_settings = ClusteringSettings(
        min_clusters=config.clustering.min_clusters,
        max_clusters=config.clustering.max_clusters,
        min_articles_per_cluster=config.clustering.min_articles_per_cluster,
        label_topic_count=config.clustering.label_topic_count,
        random_state=config.clustering.random_state,
    )
    clusters = summarize_clusters(sources, build_interest_clusters(sources, clustering_settings))

    profile = build_interest_profile(session, target_user_id, now, settings)
    candidates = load_candidates(session, target_user_id, now, settings)
    scored = rank_candidates(candidates, profile, scoring_settings, now)
    page_size = config.limits.default_page_size
    composed = compose_feed_with_stats(scored, scoring_settings, page_size)
    feed = summarize_feed_slots(composed.stats, candidate_count=len(scored), page_size=page_size)

    return Measurements(body_length=body_length, clusters=clusters, feed=feed)
