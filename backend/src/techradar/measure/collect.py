"""4 項目の計測を DB から集める（Issue #74、Issue #87）。

各集計そのものは `body_length` / `clusters` / `feed_slots` / `novelty` が持つ。ここは DB
から必要な入力を読み、既存の実装（クラスタ構築・採点・構成比適用）を呼び出して結果へ
橋渡しする。

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
from techradar.interest.clusters import build_interest_clusters
from techradar.interest.service import load_cluster_sources
from techradar.measure.body_length import load_body_lengths, summarize_body_lengths
from techradar.measure.clusters import summarize_clusters
from techradar.measure.feed_slots import summarize_feed_slots
from techradar.measure.novelty import summarize_novelty
from techradar.measure.report import Measurements
from techradar.recommendation.composition import compose_feed_with_stats
from techradar.recommendation.config import clustering_settings_from_config, get_scoring_config
from techradar.recommendation.ranking import rank_candidates
from techradar.recommendation.service import build_interest_profile, load_candidates


def collect_measurements(
    session: Session,
    *,
    settings: Settings,
    now: datetime,
    user_id: uuid.UUID | None = None,
) -> Measurements:
    """4 項目の計測結果を集める。データが無くても例外にしない。

    `session` は読み取り専用で渡すこと（`measure.session.read_only_session`）。この関数は
    書き込みを行わないが、呼び出し先が増えたときの担保は DB 側のトランザクション属性に
    置いている。書き込み可能なセッションを渡すと、その担保が外れる。
    """
    target_user_id = user_id if user_id is not None else settings.default_user_id
    config = get_scoring_config()
    scoring_settings = config.to_settings()

    body_length = summarize_body_lengths(
        load_body_lengths(session), limit=MAX_ANALYSIS_BODY_CHARACTERS
    )

    sources = load_cluster_sources(session, target_user_id, now)
    clustering_settings = clustering_settings_from_config(config)
    built_clusters = build_interest_clusters(sources, clustering_settings)
    clusters = summarize_clusters(sources, built_clusters)

    # `build_interest_profile` は既定では同じ user・同じ now に対して KMeans を
    # 自前で再実行する（Issue #89）。上で `summarize_clusters` 用に既に構築した
    # `built_clusters` をそのまま渡し、同じクラスタリングを 1 回の計測で 2 度
    # 走らせない（MR !91 self review）。
    profile = build_interest_profile(
        session, target_user_id, now, settings, clusters=built_clusters
    )
    candidates = load_candidates(session, target_user_id, now, settings)
    scored = rank_candidates(candidates, profile, scoring_settings, now)
    # 本番の DISCOVER 生成（`recommendation.service.generate_recommendations`）と同じ
    # `feed_run_size` を渡す。`default_page_size` は API の 1 ページ分の表示件数であって
    # 構成比の適用単位ではない。枠の定員は `page_size × 比率` で決まるため、ここを取り違えると
    # 定員が本番の 1/5 になり、縮退の起きやすさが実際と食い違う。
    page_size = config.limits.feed_run_size
    composed = compose_feed_with_stats(scored, scoring_settings, page_size)
    feed = summarize_feed_slots(composed.stats, candidate_count=len(scored), page_size=page_size)

    novelty = summarize_novelty(scored, scoring_settings)

    return Measurements(body_length=body_length, clusters=clusters, feed=feed, novelty=novelty)
