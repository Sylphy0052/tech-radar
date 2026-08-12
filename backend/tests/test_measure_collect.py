"""DB から 3 項目をまとめて集める処理（`techradar.measure.collect`）のテスト（Issue #74）。

各集計の値の正しさは純粋関数側のテストで固定済み。ここでは DB から読んで組み立てる
経路が、データが無い状態でも落ちないこと、データがあれば各集計へ流れることを確かめる。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from techradar.config import get_settings
from techradar.db.enums import ArticleOrigin
from techradar.db.models import Article, UserArticle
from techradar.measure.collect import collect_measurements
from techradar.recommendation.config import get_scoring_config

_NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)


def _article(session: Session, *, slug: str, body: str, embedding: list[float] | None) -> Article:
    article = Article(
        canonical_url=f"https://example.com/{slug}",
        original_url=f"https://example.com/{slug}",
        source_domain="example.com",
        title=slug,
        body=body,
        topics=["kubernetes"],
        technologies=["k8s"],
        embedding=embedding,
        analysis_status="completed",
        published_at=_NOW - timedelta(days=1),
        fetched_at=_NOW - timedelta(days=1),
    )
    session.add(article)
    session.flush()
    return article


class TestCollectMeasurements:
    def test_returns_empty_measurements_for_empty_database(self, db_session: Session) -> None:
        """データが 1 件も無くても異常終了しない。データが揃う前に実行されるため。"""
        measurements = collect_measurements(db_session, settings=get_settings(), now=_NOW)

        assert measurements.body_length.article_count == 0
        assert measurements.clusters.source_count == 0
        assert measurements.feed.candidate_count == 0

    def test_counts_bodies_of_stored_articles(self, db_session: Session) -> None:
        _article(db_session, slug="a", body="x" * 100, embedding=None)

        measurements = collect_measurements(db_session, settings=get_settings(), now=_NOW)

        assert measurements.body_length.article_count == 1
        assert measurements.body_length.max_length == 100

    def test_builds_clusters_from_interest_articles(self, db_session: Session) -> None:
        """関心記事に embedding があればクラスタ化の対象になる。"""
        settings = get_settings()
        dimensions = settings.embedding_dimensions
        for index in range(3):
            article = _article(
                db_session,
                slug=f"c{index}",
                body="x" * 100,
                embedding=[float(index)] + [0.0] * (dimensions - 1),
            )
            db_session.add(
                UserArticle(
                    user_id=settings.default_user_id,
                    article_id=article.id,
                    origin=ArticleOrigin.MANUAL.value,
                    interest_weight=1.0,
                )
            )
        db_session.flush()

        measurements = collect_measurements(db_session, settings=settings, now=_NOW)

        assert measurements.clusters.source_count == 3
        assert measurements.clusters.cluster_count >= 1
        assert sum(c.article_count for c in measurements.clusters.clusters) == 3

    def test_page_size_matches_the_discover_run(self, db_session: Session) -> None:
        """本番の DISCOVER 生成と同じ件数で構成比を適用する。

        枠の定員は `page_size × 比率` で決まる。`default_page_size`（API の 1 ページ分）を
        渡すと定員が本番の 1/5 になり、縮退の起きやすさが実際と食い違う。
        """
        measurements = collect_measurements(db_session, settings=get_settings(), now=_NOW)

        assert measurements.feed.page_size == get_scoring_config().limits.feed_run_size

    def test_does_not_write_recommendations(self, db_session: Session) -> None:
        """計測は推薦を保存しない。`generate_recommendations` と違い run を作らない。"""
        _article(db_session, slug="d", body="x" * 100, embedding=None)

        collect_measurements(db_session, settings=get_settings(), now=_NOW)

        run_count = db_session.execute(
            text("select count(*) from recommendation_runs")
        ).scalar_one()
        recommendation_count = db_session.execute(
            text("select count(*) from recommendations")
        ).scalar_one()
        assert run_count == 0
        assert recommendation_count == 0

    def test_accepts_an_explicit_user_id(self, db_session: Session) -> None:
        """利用者を指定できる。将来マルチユーザー化したときに測り分けられるようにする。"""
        measurements = collect_measurements(
            db_session, settings=get_settings(), now=_NOW, user_id=uuid.uuid4()
        )

        assert measurements.clusters.source_count == 0
