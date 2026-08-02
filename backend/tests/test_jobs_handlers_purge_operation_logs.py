"""`purge_operation_logs` ジョブハンドラを検証する結合テスト（Issue #19）。

`operation_logs` は保持期間 90 日（`PROJECT_SPEC.md` §24 / docs/decisions.md）だが、
実際に削除する実行主体が無かった。ここでは保持期間の境界と、保持日数が設定値で
変わることを確認する。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.db.models import OperationLog
from techradar.jobs.handlers.purge_operation_logs import (
    process_purge_operation_logs,
    purge_expired_operation_logs,
)
from techradar.jobs.registry import JobContext


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_context(payload: dict[str, object] | None = None) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.PURGE_OPERATION_LOGS,
        payload=payload or {},
        attempts=0,
    )


def add_log(session: Session, *, operation: str, created_at: datetime) -> OperationLog:
    """指定時刻の `operation_log` を1件作る。

    `created_at` は通常 DB 既定（`now()`）で入るが、保持期間の境界を検証するには
    任意の時刻を置く必要があるため明示的に指定する。
    """
    log = OperationLog(operation=operation, status="completed", created_at=created_at)
    session.add(log)
    session.flush()
    return log


def remaining_operations(session: Session) -> set[str]:
    return set(session.scalars(select(OperationLog.operation)).all())


class TestPurgeExpiredOperationLogs:
    def test_deletes_only_logs_older_than_the_retention_period(self, db_session: Session) -> None:
        """受入基準: 保持期間を超えたログのみが削除される。"""
        # Arrange
        now = datetime.now(UTC)
        add_log(db_session, operation="expired", created_at=now - timedelta(days=91))
        add_log(db_session, operation="fresh", created_at=now - timedelta(days=1))

        # Act
        deleted = purge_expired_operation_logs(db_session, retention_days=90, now=now)

        # Assert
        assert deleted == 1
        assert remaining_operations(db_session) == {"fresh"}

    def test_keeps_logs_inside_the_retention_period(self, db_session: Session) -> None:
        """受入基準: 保持期間内のログは削除されない。"""
        # Arrange
        now = datetime.now(UTC)
        add_log(db_session, operation="recent", created_at=now - timedelta(days=1))
        add_log(db_session, operation="almost_expired", created_at=now - timedelta(days=89))

        # Act
        deleted = purge_expired_operation_logs(db_session, retention_days=90, now=now)

        # Assert
        assert deleted == 0
        assert remaining_operations(db_session) == {"recent", "almost_expired"}

    def test_keeps_the_log_sitting_exactly_on_the_cutoff(self, db_session: Session) -> None:
        """境界はちょうど cutoff の行を残す（保持期間「を超えた」ものだけを消すため）。"""
        # Arrange
        now = datetime.now(UTC)
        add_log(db_session, operation="on_cutoff", created_at=now - timedelta(days=90))
        add_log(
            db_session,
            operation="just_past_cutoff",
            created_at=now - timedelta(days=90, seconds=1),
        )

        # Act
        deleted = purge_expired_operation_logs(db_session, retention_days=90, now=now)

        # Assert
        assert deleted == 1
        assert remaining_operations(db_session) == {"on_cutoff"}

    def test_uses_the_configured_retention_days_for_the_cutoff(self, db_session: Session) -> None:
        """受入基準: 保持日数が設定値で変更できる。"""
        # Arrange
        now = datetime.now(UTC)
        add_log(db_session, operation="older_than_7_days", created_at=now - timedelta(days=8))
        add_log(db_session, operation="within_7_days", created_at=now - timedelta(days=6))

        # Act
        deleted = purge_expired_operation_logs(db_session, retention_days=7, now=now)

        # Assert
        assert deleted == 1
        assert remaining_operations(db_session) == {"within_7_days"}


class TestProcessPurgeOperationLogs:
    def test_purges_using_the_retention_days_from_settings(self, db_session: Session) -> None:
        """ジョブ経由でも設定値の保持日数が使われる。"""
        # Arrange
        now = datetime.now(UTC)
        add_log(db_session, operation="expired", created_at=now - timedelta(days=31))
        add_log(db_session, operation="fresh", created_at=now - timedelta(days=29))
        settings = Settings(_env_file=None, log_retention_days=30)

        # Act
        process_purge_operation_logs(db_session, make_context(), settings)

        # Assert
        assert remaining_operations(db_session) == {"fresh"}

    def test_does_nothing_when_no_log_is_expired(self, db_session: Session) -> None:
        """削除対象が無くても失敗しない（巡回のたびに呼ばれるため）。"""
        # Arrange
        add_log(db_session, operation="fresh", created_at=datetime.now(UTC))
        settings = Settings(_env_file=None)

        # Act
        process_purge_operation_logs(db_session, make_context(), settings)

        # Assert
        assert remaining_operations(db_session) == {"fresh"}
