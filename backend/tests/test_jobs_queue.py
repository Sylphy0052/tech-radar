"""ジョブキュー層（`techradar.jobs`）の振る舞いテスト。"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article, Job, OperationLog
from techradar.jobs.logging import record_job_event
from techradar.jobs.queue import (
    MAX_LAST_ERROR_LENGTH,
    claim_next,
    complete,
    enqueue,
    fail,
    reclaim_stale,
    release,
)
from techradar.jobs.status import RUNNING_STATUSES, running_status_for


@pytest.fixture
def independent_sessions(
    migrated_engine: Engine,
) -> Iterator[tuple[Session, Session, Callable[[JobType], Job]]]:
    """互いに独立した2つのDB接続と、コミット済みジョブを作るヘルパーを提供する。

    `db_session` フィクスチャは1本の接続を共有し外側トランザクションでロールバックする
    作りのため、2つの接続が同時に `FOR UPDATE SKIP LOCKED` を行う二重取得防止のテストには
    使えない。ここでは接続ごとに独立したトランザクションを張り、テストで作成した
    ジョブ行の後始末（削除・接続クローズ）まで責任を持つ。
    """
    connection_a = migrated_engine.connect()
    connection_b = migrated_engine.connect()
    session_a = Session(bind=connection_a, expire_on_commit=False)
    session_b = Session(bind=connection_b, expire_on_commit=False)
    created_job_ids: list[uuid.UUID] = []

    def create_committed_job(job_type: JobType) -> Job:
        """別接続・別トランザクションでジョブを作成しコミットする。

        session_a / session_b のどちらから見ても存在する行にするため、
        claim_next を試す前にコミットで確定させておく必要がある。
        """
        with Session(bind=migrated_engine) as setup_session:
            job = enqueue(setup_session, job_type)
            setup_session.commit()
            created_job_ids.append(job.id)
            return job

    try:
        yield session_a, session_b, create_committed_job
    finally:
        session_a.close()
        session_b.close()
        connection_a.close()
        connection_b.close()
        if created_job_ids:
            with Session(bind=migrated_engine) as cleanup_session:
                cleanup_session.execute(delete(Job).where(Job.id.in_(created_job_ids)))
                cleanup_session.commit()


def test_enqueue_creates_a_pending_job(db_session: Session) -> None:
    # Arrange / Act
    job = enqueue(db_session, JobType.FETCH_ARTICLE, payload={"url": "https://example.com"})

    # Assert
    assert job.status == JobStatus.PENDING.value
    assert job.payload == {"url": "https://example.com"}
    assert job.attempts == 0


def test_claim_next_returns_none_when_no_job_is_available(db_session: Session) -> None:
    # Arrange / Act / Assert
    assert claim_next(db_session) is None


def test_claim_next_returns_the_enqueued_job(db_session: Session) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)

    # Act
    claimed = claim_next(db_session)

    # Assert
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.started_at is not None


@pytest.mark.parametrize(
    ("job_type", "expected_status"),
    [
        (JobType.FETCH_ARTICLE, JobStatus.FETCHING),
        (JobType.ANALYZE_ARTICLE, JobStatus.ANALYZING),
        (JobType.EMBED_ARTICLE, JobStatus.ANALYZING),
        (JobType.CRAWL_SOURCES, JobStatus.SEARCHING),
        (JobType.GENERATE_FEED, JobStatus.SEARCHING),
    ],
)
def test_claim_next_sets_the_running_status_for_the_job_type(
    db_session: Session, job_type: JobType, expected_status: JobStatus
) -> None:
    # Arrange
    enqueue(db_session, job_type)

    # Act
    claimed = claim_next(db_session)

    # Assert
    assert claimed is not None
    assert claimed.status == expected_status.value


def test_claim_next_skips_a_job_whose_available_at_is_in_the_future(db_session: Session) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    job.available_at = datetime.now(UTC) + timedelta(hours=1)
    db_session.flush()

    # Act / Assert
    assert claim_next(db_session) is None


def test_second_claim_returns_none_when_first_holds_the_row_lock(
    independent_sessions: tuple[Session, Session, Callable[[JobType], Job]],
) -> None:
    """`FOR UPDATE SKIP LOCKED` により同一ジョブが2重取得されないこと（受入基準）。"""
    # Arrange
    session_a, session_b, create_committed_job = independent_sessions
    create_committed_job(JobType.FETCH_ARTICLE)

    # Act
    claimed_by_a = claim_next(session_a)
    claimed_by_b = claim_next(session_b)

    # Assert
    assert claimed_by_a is not None
    assert claimed_by_b is None

    session_a.rollback()
    session_b.rollback()


def test_fail_reschedules_with_exponential_backoff_until_max_attempts_then_fails(
    db_session: Session,
) -> None:
    """3回失敗で failed になり、1・2回目は available_at が指数的に伸びること（受入基準）。"""
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)
    before_first_failure = datetime.now(UTC)

    # Act: 1回目の失敗
    fail(db_session, job, "timeout", max_attempts=3, backoff_seconds=5.0)

    # Assert: pending に戻り、約5秒後に再試行可能
    assert job.status == JobStatus.PENDING.value
    assert job.attempts == 1
    assert job.started_at is None
    assert job.finished_at is None
    assert job.last_error == "timeout"
    first_delay = (job.available_at - before_first_failure).total_seconds()
    assert 4.5 <= first_delay <= 6.5

    # Act: 2回目の失敗
    claim_next(db_session, now=job.available_at)
    before_second_failure = datetime.now(UTC)
    fail(db_session, job, "timeout again", max_attempts=3, backoff_seconds=5.0)

    # Assert: 約10秒後に再試行可能（指数バックオフ）
    assert job.status == JobStatus.PENDING.value
    assert job.attempts == 2
    second_delay = (job.available_at - before_second_failure).total_seconds()
    assert 9.0 <= second_delay <= 11.0

    # Act: 3回目の失敗
    claim_next(db_session, now=job.available_at)
    fail(db_session, job, "final failure", max_attempts=3, backoff_seconds=5.0)

    # Assert: 上限到達で failed
    assert job.status == JobStatus.FAILED.value
    assert job.attempts == 3
    assert job.last_error == "final failure"
    assert job.finished_at is not None


def test_fail_marks_the_job_failed_immediately_when_not_retryable(db_session: Session) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)

    # Act
    fail(db_session, job, "invalid payload", max_attempts=3, backoff_seconds=5.0, retryable=False)

    # Assert: 試行回数を残したまま即 failed
    assert job.status == JobStatus.FAILED.value
    assert job.attempts == 1
    assert job.finished_at is not None


def test_fail_truncates_a_last_error_that_exceeds_the_limit(db_session: Session) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)
    long_error = "e" * (MAX_LAST_ERROR_LENGTH + 500)

    # Act
    fail(db_session, job, long_error, max_attempts=1, backoff_seconds=1.0, retryable=False)

    # Assert
    assert job.last_error is not None
    assert len(job.last_error) == MAX_LAST_ERROR_LENGTH


def test_release_returns_the_job_to_pending_without_incrementing_attempts(
    db_session: Session,
) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)
    available_before_release = job.available_at

    # Act
    rolled_back = release(db_session, job)

    # Assert
    assert rolled_back is True
    assert job.status == JobStatus.PENDING.value
    assert job.started_at is None
    assert job.attempts == 0
    assert job.available_at == available_before_release


def test_release_does_not_overwrite_a_job_that_already_completed(db_session: Session) -> None:
    """CRITICAL: release がスレッド側で先に completed へ進んだジョブを上書きしないこと。"""
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)
    complete(db_session, job)

    # Act
    rolled_back = release(db_session, job)

    # Assert: pending へ巻き戻さず、completed のまま
    assert rolled_back is False
    assert job.status == JobStatus.COMPLETED.value


def test_release_does_not_overwrite_a_job_that_already_failed(db_session: Session) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)
    fail(db_session, job, "boom", max_attempts=1, backoff_seconds=1.0, retryable=False)

    # Act
    rolled_back = release(db_session, job)

    # Assert
    assert rolled_back is False
    assert job.status == JobStatus.FAILED.value


def test_release_is_a_no_op_for_a_job_that_is_already_pending(db_session: Session) -> None:
    # Arrange: claim せずそのまま pending のジョブに release を呼ぶ
    job = enqueue(db_session, JobType.FETCH_ARTICLE)

    # Act
    rolled_back = release(db_session, job)

    # Assert
    assert rolled_back is False
    assert job.status == JobStatus.PENDING.value


def test_reclaim_stale_returns_running_jobs_to_pending_and_reports_the_count(
    db_session: Session,
) -> None:
    """実行中 status のジョブだけを pending に戻し、件数を返すこと（completed/failed は対象外）。"""
    # Arrange: fetching / analyzing / searching をそれぞれ1件ずつ作る
    fetching_job = enqueue(db_session, JobType.FETCH_ARTICLE)
    claim_next(db_session)

    analyzing_job = enqueue(db_session, JobType.ANALYZE_ARTICLE)
    claim_next(db_session)

    searching_job = enqueue(db_session, JobType.CRAWL_SOURCES)
    claim_next(db_session)

    completed_job = enqueue(db_session, JobType.GENERATE_FEED)
    claim_next(db_session)
    complete(db_session, completed_job)

    failed_job = enqueue(db_session, JobType.EMBED_ARTICLE)
    claim_next(db_session)
    fail(db_session, failed_job, "boom", max_attempts=1, backoff_seconds=1.0)

    # Act
    reclaimed_count = reclaim_stale(db_session)

    # Assert
    db_session.refresh(fetching_job)
    db_session.refresh(analyzing_job)
    db_session.refresh(searching_job)
    db_session.refresh(completed_job)
    db_session.refresh(failed_job)

    assert reclaimed_count == 3
    assert fetching_job.status == JobStatus.PENDING.value
    assert fetching_job.started_at is None
    assert analyzing_job.status == JobStatus.PENDING.value
    assert searching_job.status == JobStatus.PENDING.value
    assert completed_job.status == JobStatus.COMPLETED.value
    assert failed_job.status == JobStatus.FAILED.value


def test_record_job_event_persists_a_row_linked_to_the_job(db_session: Session) -> None:
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE, payload={"url": "https://example.com"})

    # Act
    log = record_job_event(db_session, job=job, status=JobStatus.COMPLETED.value, duration_ms=120)

    # Assert
    assert log.job_id == job.id
    assert log.operation == JobType.FETCH_ARTICLE.value
    assert log.status == JobStatus.COMPLETED.value
    assert log.duration_ms == 120
    stored = db_session.scalar(select(OperationLog).where(OperationLog.id == log.id))
    assert stored is not None


def test_record_job_event_sets_article_id_when_payload_contains_a_valid_uuid(
    db_session: Session,
) -> None:
    # Arrange: article_id 列は articles への外部キーのため、実在する記事を用意する
    article = Article(
        canonical_url="https://example.com/article",
        original_url="https://example.com/article",
        title="Example Article",
        source_domain="example.com",
    )
    db_session.add(article)
    db_session.flush()
    job = enqueue(db_session, JobType.ANALYZE_ARTICLE, payload={"article_id": str(article.id)})

    # Act
    log = record_job_event(db_session, job=job, status=JobStatus.COMPLETED.value)

    # Assert
    assert log.article_id == article.id


def test_record_job_event_ignores_an_invalid_article_id_without_raising(
    db_session: Session,
) -> None:
    # Arrange
    job = enqueue(db_session, JobType.ANALYZE_ARTICLE, payload={"article_id": "not-a-uuid"})

    # Act
    log = record_job_event(db_session, job=job, status=JobStatus.FAILED.value, error_reason="x")

    # Assert
    assert log.article_id is None


def test_record_job_event_truncates_a_long_error_reason(db_session: Session) -> None:
    """error_reason も last_error と同じ上限で切り詰め、一貫させること。"""
    # Arrange
    job = enqueue(db_session, JobType.FETCH_ARTICLE)
    long_error = "e" * (MAX_LAST_ERROR_LENGTH + 500)

    # Act
    log = record_job_event(
        db_session, job=job, status=JobStatus.FAILED.value, error_reason=long_error
    )

    # Assert
    assert log.error_reason is not None
    assert len(log.error_reason) == MAX_LAST_ERROR_LENGTH


def test_running_status_for_maps_every_job_type_to_a_running_status() -> None:
    # Arrange / Act / Assert
    for job_type in JobType:
        assert running_status_for(job_type) in RUNNING_STATUSES
