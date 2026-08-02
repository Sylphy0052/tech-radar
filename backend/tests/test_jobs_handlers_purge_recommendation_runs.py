"""`purge_recommendation_runs` ジョブハンドラを検証する結合テスト（Issue #28）。

`recommendation_runs` の保持期間は設定値（既定 30 日、`config.py` の
`recommendation_run_retention_days`）だが、`operation_logs` と同様に実際に
削除する実行主体が無かった。ここでは保持期間の境界と、紐づく `recommendations`
が CASCADE で一緒に消えることを確認する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import JobType, RecommendationMode
from techradar.db.models import Article, Recommendation, RecommendationRun
from techradar.jobs.handlers import purge_recommendation_runs as purge_recommendation_runs_module
from techradar.jobs.handlers.purge_recommendation_runs import (
    process_purge_recommendation_runs,
    purge_expired_recommendation_runs,
)
from techradar.jobs.registry import JobContext


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_context(payload: dict[str, object] | None = None) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.PURGE_RECOMMENDATION_RUNS,
        payload=payload or {},
        attempts=0,
    )


def add_article(session: Session) -> Article:
    """`recommendations.article_id` の外部キー先として最低限の記事を1件作る。"""
    suffix = uuid.uuid4().hex[:8]
    article = Article(
        canonical_url=f"https://example.com/article-{suffix}",
        original_url=f"https://example.com/article-{suffix}",
        title="Example Article",
        source_domain="example.com",
    )
    session.add(article)
    session.flush()
    return article


def add_run(
    session: Session, *, generated_at: datetime, with_recommendation: bool = False
) -> RecommendationRun:
    """指定時刻の `recommendation_run` を1件作る。

    `generated_at` は通常 DB 既定（`now()`）で入るが、保持期間の境界を検証する
    には任意の時刻を置く必要があるため明示的に指定する。
    """
    run = RecommendationRun(
        user_id=uuid.uuid4(),
        mode=RecommendationMode.DISCOVER,
        generated_at=generated_at,
    )
    session.add(run)
    session.flush()
    if with_recommendation:
        article = add_article(session)
        session.add(
            Recommendation(run_id=run.id, article_id=article.id, score=0.5, reasons={}, rank=1)
        )
        session.flush()
    return run


def remaining_run_ids(session: Session) -> set[uuid.UUID]:
    return set(session.scalars(select(RecommendationRun.id)).all())


def remaining_recommendation_run_ids(session: Session) -> set[uuid.UUID]:
    return set(session.scalars(select(Recommendation.run_id)).all())


class TestPurgeExpiredRecommendationRuns:
    def test_deletes_only_runs_older_than_the_retention_period(self, db_session: Session) -> None:
        """受入基準: 保持期間を超えた run のみが削除される。"""
        # Arrange
        now = datetime.now(UTC)
        expired_id = add_run(db_session, generated_at=now - timedelta(days=31)).id
        fresh_id = add_run(db_session, generated_at=now - timedelta(days=1)).id

        # Act
        deleted = purge_expired_recommendation_runs(db_session, retention_days=30, now=now)

        # Assert
        assert deleted == 1
        assert remaining_run_ids(db_session) == {fresh_id}
        assert expired_id not in remaining_run_ids(db_session)

    def test_keeps_runs_inside_the_retention_period(self, db_session: Session) -> None:
        """受入基準: 保持期間内の run は削除されない。"""
        # Arrange
        now = datetime.now(UTC)
        recent = add_run(db_session, generated_at=now - timedelta(days=1))
        almost_expired = add_run(db_session, generated_at=now - timedelta(days=29))

        # Act
        deleted = purge_expired_recommendation_runs(db_session, retention_days=30, now=now)

        # Assert
        assert deleted == 0
        assert remaining_run_ids(db_session) == {recent.id, almost_expired.id}

    def test_keeps_the_run_sitting_exactly_on_the_cutoff(self, db_session: Session) -> None:
        """境界はちょうど cutoff の行を残す（保持期間「を超えた」ものだけを消すため）。"""
        # Arrange
        now = datetime.now(UTC)
        on_cutoff_id = add_run(db_session, generated_at=now - timedelta(days=30)).id
        just_past_cutoff_id = add_run(
            db_session, generated_at=now - timedelta(days=30, seconds=1)
        ).id

        # Act
        deleted = purge_expired_recommendation_runs(db_session, retention_days=30, now=now)

        # Assert
        assert deleted == 1
        assert remaining_run_ids(db_session) == {on_cutoff_id}
        assert just_past_cutoff_id not in remaining_run_ids(db_session)

    def test_uses_the_configured_retention_days_for_the_cutoff(self, db_session: Session) -> None:
        """受入基準: 保持日数が設定値で変更できる。"""
        # Arrange
        now = datetime.now(UTC)
        add_run(db_session, generated_at=now - timedelta(days=8))
        within = add_run(db_session, generated_at=now - timedelta(days=6))

        # Act
        deleted = purge_expired_recommendation_runs(db_session, retention_days=7, now=now)

        # Assert
        assert deleted == 1
        assert remaining_run_ids(db_session) == {within.id}

    def test_cascades_the_deletion_to_recommendations_of_the_expired_run(
        self, db_session: Session
    ) -> None:
        """受入基準: 紐づく `recommendations` は `run_id` の CASCADE で一緒に消える。"""
        # Arrange
        now = datetime.now(UTC)
        expired_id = add_run(
            db_session, generated_at=now - timedelta(days=31), with_recommendation=True
        ).id
        fresh_id = add_run(
            db_session, generated_at=now - timedelta(days=1), with_recommendation=True
        ).id

        # Act
        deleted = purge_expired_recommendation_runs(db_session, retention_days=30, now=now)

        # Assert
        assert deleted == 1
        assert remaining_recommendation_run_ids(db_session) == {fresh_id}
        assert expired_id not in remaining_recommendation_run_ids(db_session)


class TestProcessPurgeRecommendationRuns:
    def test_purges_using_the_retention_days_from_settings(self, db_session: Session) -> None:
        """ジョブ経由でも設定値の保持日数が使われる。"""
        # Arrange
        now = datetime.now(UTC)
        add_run(db_session, generated_at=now - timedelta(days=31))
        fresh = add_run(db_session, generated_at=now - timedelta(days=29))
        settings = Settings(_env_file=None, recommendation_run_retention_days=30)

        # Act
        process_purge_recommendation_runs(db_session, make_context(), settings)

        # Assert
        assert remaining_run_ids(db_session) == {fresh.id}

    def test_does_nothing_when_no_run_is_expired(self, db_session: Session) -> None:
        """削除対象が無くても失敗しない（巡回のたびに呼ばれるため）。"""
        # Arrange
        fresh = add_run(db_session, generated_at=datetime.now(UTC))
        settings = Settings(_env_file=None)

        # Act
        process_purge_recommendation_runs(db_session, make_context(), settings)

        # Assert
        assert remaining_run_ids(db_session) == {fresh.id}

    def test_logs_the_deleted_count_at_info_level(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 削除件数を含む INFO ログを出す。

        `caplog` ではなく `purge_recommendation_runs.logger.info` を直接差し替えて
        検証する。`migrated_engine`（セッションスコープ）経由で alembic の
        `env.py` が呼ぶ `logging.config.fileConfig`（既定で
        `disable_existing_loggers=True`）により、このモジュールの logger
        インスタンスがセッション内で disabled になりうるため（`caplog` が記録を
        拾えないことがある、`test_jobs_worker.py` と同じ事情）、ロガーの
        有効/無効に依存しない検証にする。
        """
        # Arrange
        info_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            purge_recommendation_runs_module.logger,
            "info",
            lambda *args, **_kwargs: info_calls.append(args),
        )
        now = datetime.now(UTC)
        add_run(db_session, generated_at=now - timedelta(days=31))
        settings = Settings(_env_file=None, recommendation_run_retention_days=30)
        context = make_context()

        # Act
        process_purge_recommendation_runs(db_session, context, settings)

        # Assert
        assert info_calls == [
            (
                "purge_recommendation_runs.deleted job_id=%s count=%s retention_days=%s",
                context.job_id,
                1,
                30,
            )
        ]
