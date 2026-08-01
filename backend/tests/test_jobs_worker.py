"""`techradar.jobs.worker.JobWorker` の振る舞いテスト。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job, OperationLog
from techradar.jobs.queue import claim_next, enqueue
from techradar.jobs.registry import JobContext, JobHandlerRegistry
from techradar.jobs.worker import JobWorker


@pytest.fixture
def worker_session_factory(migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """ワーカー専用の独立したエンジンから作った sessionmaker を提供する。

    `JobWorker` は DB 操作のたびに `session_factory()` で新しいセッションを開き、
    複数のワーカーコルーチンが同時に別々の接続を使う設計のため、`db_session`
    フィクスチャ（単一接続・ロールバック方式）は使えない。テスト用 DB を指す
    専用エンジン（コネクションプール）をここで用意し、後始末として作成された
    jobs / operation_logs 行の削除とエンジンの dispose（接続のクローズ）まで
    責任を持つ。
    """
    engine = create_engine(migrated_engine.url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        with Session(bind=migrated_engine) as cleanup_session:
            cleanup_session.execute(delete(OperationLog))
            cleanup_session.execute(delete(Job))
            cleanup_session.commit()
        engine.dispose()


def _fast_settings(**overrides: Any) -> Settings:
    """テストが短時間で終わるよう、待機系の秒数を小さくした設定を返す。"""
    defaults: dict[str, Any] = {
        "worker_concurrency": 1,
        "worker_poll_interval_seconds": 0.02,
        "worker_shutdown_grace_seconds": 0.1,
        "job_max_attempts": 3,
        "job_retry_backoff_seconds": 0.01,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _enqueue_committed(
    session_factory: sessionmaker[Session],
    job_type: JobType,
    payload: dict[str, Any] | None = None,
) -> uuid.UUID:
    """ワーカーから見えるよう、別セッションでコミット済みのジョブを作る。"""
    with session_factory() as session:
        job = enqueue(session, job_type, payload)
        session.commit()
        return job.id


async def _wait_for_job_status(
    session_factory: sessionmaker[Session],
    job_id: uuid.UUID,
    expected: JobStatus,
    *,
    timeout: float = 2.0,
) -> Job:
    """ジョブが期待する status になるまで短い間隔でポーリングする。

    ワーカーの DB 反映は別スレッドで非同期に進むため、固定秒数の sleep で
    完了を仮定せず、状態が実際に変わるまで明示的に待つ。
    """
    deadline = time.monotonic() + timeout
    while True:
        with session_factory() as session:
            job = session.get(Job, job_id)
            if job is not None and job.status == expected.value:
                return job
        if time.monotonic() >= deadline:
            message = f"job {job_id} did not reach status={expected.value} within {timeout}s"
            raise AssertionError(message)
        await asyncio.sleep(0.01)


async def test_worker_processes_a_job_with_the_registered_handler(
    worker_session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    received: list[JobContext] = []
    handled = asyncio.Event()

    async def handler(context: JobContext) -> None:
        received.append(context)
        handled.set()

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings()
    job_id = _enqueue_committed(
        worker_session_factory, JobType.FETCH_ARTICLE, {"url": "https://example.com"}
    )
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    # Act
    await worker.start()
    try:
        await asyncio.wait_for(handled.wait(), timeout=2.0)
        job = await _wait_for_job_status(worker_session_factory, job_id, JobStatus.COMPLETED)
    finally:
        await worker.stop()

    # Assert
    assert len(received) == 1
    assert received[0].job_id == job_id
    assert received[0].job_type == JobType.FETCH_ARTICLE
    assert received[0].payload == {"url": "https://example.com"}
    assert job.status == JobStatus.COMPLETED.value


async def test_worker_fails_immediately_for_an_unregistered_job_type(
    worker_session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 何も登録しない空のレジストリ
    registry = JobHandlerRegistry()
    settings = _fast_settings()
    job_id = _enqueue_committed(worker_session_factory, JobType.GENERATE_FEED)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    # Act
    await worker.start()
    try:
        job = await _wait_for_job_status(worker_session_factory, job_id, JobStatus.FAILED)
    finally:
        await worker.stop()

    # Assert: リトライされず即 failed になり、理由が last_error に残る
    assert job.attempts == 1
    assert job.last_error is not None
    assert "generate_feed" in job.last_error


async def test_worker_fails_the_job_and_records_the_error_when_the_handler_raises(
    worker_session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    async def handler(context: JobContext) -> None:
        raise RuntimeError("boom")

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(job_max_attempts=1)
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    # Act
    await worker.start()
    try:
        job = await _wait_for_job_status(worker_session_factory, job_id, JobStatus.FAILED)
    finally:
        await worker.stop()

    # Assert: attempts が増え、last_error が記録される
    assert job.attempts == 1
    assert job.last_error == "boom"


async def test_stop_releases_a_job_that_exceeds_the_shutdown_grace_period(
    worker_session_factory: sessionmaker[Session],
) -> None:
    """受入基準: シャットダウンで処理中ジョブが pending に戻ること。"""
    # Arrange: 猶予を確実に超えて動き続けるハンドラ
    started = asyncio.Event()

    async def handler(context: JobContext) -> None:
        started.set()
        await asyncio.sleep(10)

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(worker_shutdown_grace_seconds=0.05)
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)
    await worker.start()
    await asyncio.wait_for(started.wait(), timeout=2.0)

    # Act
    await worker.stop()

    # Assert: 猶予超過でキャンセルされ、pending に巻き戻っている
    with worker_session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.PENDING.value
        assert job.started_at is None
        assert job.attempts == 0


async def test_stop_waits_for_a_handler_that_finishes_within_the_grace_period(
    worker_session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 猶予内に終わるハンドラ
    started = asyncio.Event()

    async def handler(context: JobContext) -> None:
        started.set()
        await asyncio.sleep(0.03)

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(worker_shutdown_grace_seconds=0.5)
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)
    await worker.start()
    await asyncio.wait_for(started.wait(), timeout=2.0)

    # Act
    await worker.stop()

    # Assert: 最後まで完了して completed になる
    with worker_session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.COMPLETED.value


async def test_start_reclaims_jobs_left_in_a_running_status(
    worker_session_factory: sessionmaker[Session],
) -> None:
    # Arrange: プロセスが強制終了された場合を模して fetching のまま放置する
    with worker_session_factory() as session:
        job = enqueue(session, JobType.FETCH_ARTICLE)
        claim_next(session)
        job_id = job.id
        session.commit()

    handled = asyncio.Event()
    received_job_ids: list[uuid.UUID] = []

    async def handler(context: JobContext) -> None:
        received_job_ids.append(context.job_id)
        handled.set()

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings()
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    # Act: start() の reclaim_stale で pending に戻らない限りこの待ちはタイムアウトする
    await worker.start()
    try:
        await asyncio.wait_for(handled.wait(), timeout=2.0)
    finally:
        await worker.stop()

    # Assert
    assert received_job_ids == [job_id]


async def test_worker_concurrency_processes_jobs_in_parallel(
    worker_session_factory: sessionmaker[Session],
) -> None:
    """受入基準: 並列度が設定値どおりに効くこと。"""
    # Arrange: 2 件同時に到達しないと解放されないバリアで並行実行を証明する
    concurrent_gate = asyncio.Barrier(2)

    async def handler(context: JobContext) -> None:
        await asyncio.wait_for(concurrent_gate.wait(), timeout=2.0)

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(worker_concurrency=2)
    job_ids = [_enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE) for _ in range(2)]
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    # Act
    await worker.start()
    try:
        for job_id in job_ids:
            await _wait_for_job_status(worker_session_factory, job_id, JobStatus.COMPLETED)
    finally:
        await worker.stop()


async def test_worker_records_operation_logs_for_completed_and_failed_jobs(
    worker_session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    async def ok_handler(context: JobContext) -> None:
        return None

    async def failing_handler(context: JobContext) -> None:
        raise RuntimeError("boom")

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, ok_handler)
    registry.register(JobType.ANALYZE_ARTICLE, failing_handler)
    settings = _fast_settings(job_max_attempts=1)
    completed_job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    failed_job_id = _enqueue_committed(worker_session_factory, JobType.ANALYZE_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    # Act
    await worker.start()
    try:
        await _wait_for_job_status(worker_session_factory, completed_job_id, JobStatus.COMPLETED)
        await _wait_for_job_status(worker_session_factory, failed_job_id, JobStatus.FAILED)
    finally:
        await worker.stop()

    # Assert
    with worker_session_factory() as session:
        completed_log = session.scalar(
            select(OperationLog).where(OperationLog.job_id == completed_job_id)
        )
        failed_log = session.scalar(
            select(OperationLog).where(OperationLog.job_id == failed_job_id)
        )

    assert completed_log is not None
    assert completed_log.status == JobStatus.COMPLETED.value
    assert completed_log.duration_ms is not None

    assert failed_log is not None
    assert failed_log.status == JobStatus.FAILED.value
    assert failed_log.error_reason == "boom"
