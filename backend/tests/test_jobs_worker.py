"""`techradar.jobs.worker.JobWorker` の振る舞いテスト。"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job, OperationLog
from techradar.jobs import worker as worker_module
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
        "worker_cancel_await_timeout_seconds": 0.5,
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


async def test_stop_reclaims_a_running_job_that_the_worker_never_tracked(
    worker_session_factory: sessionmaker[Session],
) -> None:
    """CRITICAL: claim 直後にキャンセルされ worker が追跡できなかったジョブも回収されること。

    再現が難しい「claim 完走直後、_current_jobs へ登録される前にコルーチンが死ぬ」
    ケースの代わりに、既に実行中 status のジョブを worker の外側から直接仕込む
    （= worker からは存在を知りようがない、という点で同じ状況を作る）。
    """
    # Arrange
    registry = JobHandlerRegistry()
    settings = _fast_settings()
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)
    await worker.start()

    with worker_session_factory() as session:
        job = enqueue(session, JobType.FETCH_ARTICLE)
        claim_next(session)
        job_id = job.id
        session.commit()

    # Act
    await worker.stop()

    # Assert
    with worker_session_factory() as session:
        reclaimed_job = session.get(Job, job_id)
        assert reclaimed_job is not None
        assert reclaimed_job.status == JobStatus.PENDING.value
        assert reclaimed_job.started_at is None


class _FlakySessionFactory:
    """一定回数だけ例外を投げたあと、実際のセッションファクトリへ委譲するスタブ。

    DB 接続断のような一時的な障害を模す。`armed` が True になるまでは常に本物へ
    委譲する（`worker.start()` の `reclaim_stale` 呼び出しを失敗させないため）。
    """

    def __init__(self, real_factory: Callable[[], Session]) -> None:
        self._real_factory = real_factory
        self.armed = False
        self.remaining_failures = 0

    def __call__(self) -> Session:
        if self.armed and self.remaining_failures > 0:
            self.remaining_failures -= 1
            message = "db connection lost"
            raise RuntimeError(message)
        return self._real_factory()


async def test_worker_loop_continues_after_transient_session_factory_errors(
    worker_session_factory: sessionmaker[Session],
) -> None:
    """CRITICAL: DB 接続断などの想定外の例外でワーカーが1本死なないこと。"""
    # Arrange
    handled = asyncio.Event()

    async def handler(context: JobContext) -> None:
        handled.set()

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings()
    flaky_factory = _FlakySessionFactory(worker_session_factory)
    worker = JobWorker(settings=settings, registry=registry, session_factory=flaky_factory)

    # Act: start() の reclaim_stale はまだ arm していないので成功する
    await worker.start()
    flaky_factory.armed = True
    flaky_factory.remaining_failures = 3
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    try:
        # 例外のたびに poll interval だけ待って継続するため、猶予を持たせて待つ
        await asyncio.wait_for(handled.wait(), timeout=2.0)
        job = await _wait_for_job_status(worker_session_factory, job_id, JobStatus.COMPLETED)
    finally:
        await worker.stop()

    # Assert: 例外を吸収しつつ最終的にジョブを処理できている
    assert job.status == JobStatus.COMPLETED.value
    assert flaky_factory.remaining_failures == 0


async def test_stop_does_not_hang_when_a_handler_swallows_cancelled_error(
    worker_session_factory: sessionmaker[Session],
) -> None:
    """MEDIUM: ハンドラが CancelledError を握り潰しても stop() がタイムアウトで戻ること。"""
    # Arrange: キャンセルを繰り返し握り潰したあと、自発的に終わるハンドラ
    started = asyncio.Event()

    async def handler(context: JobContext) -> None:
        started.set()
        for _ in range(5):
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.05)

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(
        worker_shutdown_grace_seconds=0.05, worker_cancel_await_timeout_seconds=0.1
    )
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)
    await worker.start()
    await asyncio.wait_for(started.wait(), timeout=2.0)

    # Act: stop() 自体が無期限にブロックせず戻ってくること
    await asyncio.wait_for(worker.stop(), timeout=2.0)

    # ハンドラのバックグラウンド継続分（最大 0.25 秒）が完全に終わるのを待ち、
    # テスト終了後まで残留タスクが生き残らないようにする。
    await asyncio.sleep(0.3)

    # Assert: 取り残されたタスクが後から完走しても、release で戻した pending を
    # completed へ書き換えないこと（Issue #27）。
    with worker_session_factory() as session:
        job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.PENDING.value
    assert job.finished_at is None


async def test_a_late_write_from_an_abandoned_task_does_not_overwrite_a_released_job(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受入基準: release 済みのジョブが後から completed / failed へ上書きされないこと。

    `asyncio.to_thread` が起こしたスレッドはキャンセルできない。`_complete_job_sync`
    をスレッドで実行している最中に stop() がキャンセルした場合、コルーチンは
    CancelledError で終わる一方、スレッドはそのまま完了を書きにくる。
    """
    # Arrange: 完了記録の直前で足止めし、stop() のキャンセルを受けさせる
    reached_completion = asyncio.Event()
    allow_completion = threading.Event()

    async def handler(context: JobContext) -> None:
        return None

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(
        worker_shutdown_grace_seconds=0.05, worker_cancel_await_timeout_seconds=0.1
    )
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    original_complete_job_sync = worker._complete_job_sync

    def blocking_complete_job_sync(claimed: Any, duration_ms: int) -> None:
        # スレッド側で走る。ここで待たせている間に stop() がキャンセルと release を行う。
        reached_completion.set()
        allow_completion.wait(timeout=5.0)
        original_complete_job_sync(claimed, duration_ms)

    monkeypatch.setattr(worker, "_complete_job_sync", blocking_complete_job_sync)

    await worker.start()
    await asyncio.wait_for(reached_completion.wait(), timeout=2.0)

    # Act: 完了記録が終わる前に停止し、そのあとスレッド側の書き込みを進ませる
    await asyncio.wait_for(worker.stop(), timeout=2.0)
    allow_completion.set()
    await asyncio.sleep(0.3)

    # Assert: release で戻した pending のままで、completed に化けていない
    with worker_session_factory() as session:
        job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.PENDING.value
    assert job.finished_at is None


async def test_a_late_failure_write_from_an_abandoned_task_does_not_overwrite_a_released_job(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失敗の記録側も、release 済みのジョブを上書きしないこと（Issue #27）。

    `complete` と同じガードを共有しているが、失敗の記録では attempts が増えるため、
    通ってしまうと利用者都合の中断がリトライ回数を消費する。
    """
    # Arrange: ハンドラを失敗させ、失敗記録の直前で足止めする
    reached_failure = asyncio.Event()
    allow_failure = threading.Event()

    async def handler(context: JobContext) -> None:
        raise RuntimeError("boom")

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(
        worker_shutdown_grace_seconds=0.05, worker_cancel_await_timeout_seconds=0.1
    )
    job_id = _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    original_fail_job_sync = worker._fail_job_sync

    def blocking_fail_job_sync(
        claimed: Any, error: str, duration_ms: int, *, retryable: bool
    ) -> None:
        reached_failure.set()
        allow_failure.wait(timeout=5.0)
        original_fail_job_sync(claimed, error, duration_ms, retryable=retryable)

    monkeypatch.setattr(worker, "_fail_job_sync", blocking_fail_job_sync)

    await worker.start()
    await asyncio.wait_for(reached_failure.wait(), timeout=2.0)

    # Act
    await asyncio.wait_for(worker.stop(), timeout=2.0)
    allow_failure.set()
    await asyncio.sleep(0.3)

    # Assert: release で戻した pending のままで、attempts も増えていない
    with worker_session_factory() as session:
        job = session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.PENDING.value
    assert job.attempts == 0
    assert job.last_error is None


async def test_stop_completes_the_remaining_work_when_one_release_fails(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1件の release が失敗しても、残りの後始末と reclaim_stale を実行すること。

    停止処理を「どれか1つが転んだら全部やめる」形にすると、DB の一時的なエラー
    1件で取り残しタスクの退避もシャットダウン時の回収も飛んでしまう。
    """
    # Arrange: 2本とも猶予超過させ、最初の release だけ失敗させる
    concurrency = 2
    started = asyncio.Semaphore(0)

    async def handler(context: JobContext) -> None:
        started.release()
        await asyncio.sleep(10)

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(
        worker_concurrency=concurrency,
        worker_shutdown_grace_seconds=0.05,
        worker_cancel_await_timeout_seconds=0.1,
    )
    job_ids = [
        _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
        for _ in range(concurrency)
    ]
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)

    original_release_job_sync = worker._release_job_sync
    release_calls: list[uuid.UUID] = []

    def flaky_release_job_sync(job_id: uuid.UUID) -> bool:
        release_calls.append(job_id)
        if len(release_calls) == 1:
            message = "simulated DB error during release"
            raise RuntimeError(message)
        return original_release_job_sync(job_id)

    monkeypatch.setattr(worker, "_release_job_sync", flaky_release_job_sync)

    reclaim_calls: list[int] = []
    original_reclaim_stale_sync = worker._reclaim_stale_sync

    def counting_reclaim_stale_sync() -> int:
        reclaimed = original_reclaim_stale_sync()
        reclaim_calls.append(reclaimed)
        return reclaimed

    await worker.start()
    for _ in range(concurrency):
        await asyncio.wait_for(started.acquire(), timeout=2.0)
    # start() も reclaim_stale を呼ぶため、差し替えは起動後にする。
    monkeypatch.setattr(worker, "_reclaim_stale_sync", counting_reclaim_stale_sync)

    # Act: stop() 自体は例外を投げずに完了する
    await asyncio.wait_for(worker.stop(), timeout=3.0)

    # Assert: 両方の release が試みられ、shutdown 時の reclaim_stale も走っている
    assert len(release_calls) == concurrency
    assert len(reclaim_calls) == 1

    # 失敗しなかった側は pending へ戻り、失敗した側も reclaim_stale が回収する
    with worker_session_factory() as session:
        jobs = [session.get(Job, job_id) for job_id in job_ids]
    assert all(job is not None for job in jobs)
    assert {job.status for job in jobs if job is not None} == {JobStatus.PENDING.value}


async def test_stop_cancels_overdue_tasks_concurrently(
    worker_session_factory: sessionmaker[Session],
) -> None:
    """受入基準: 複数タスクが同時に猶予超過してもシャットダウンが直列に伸びないこと。

    キャンセル待ちを1件ずつ `await` していると、所要時間が
    `grace + N * cancel_await_timeout` まで伸びる。まとめて待てば
    `cancel_await_timeout` 1回分に収まる。
    """
    # Arrange: 2本とも猶予を超え、かつキャンセルを握り潰すハンドラ
    concurrency = 2
    cancel_await_timeout = 0.4
    started = asyncio.Semaphore(0)

    async def handler(context: JobContext) -> None:
        started.release()
        for _ in range(20):
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.05)

    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, handler)
    settings = _fast_settings(
        worker_concurrency=concurrency,
        worker_shutdown_grace_seconds=0.05,
        worker_cancel_await_timeout_seconds=cancel_await_timeout,
    )
    for _ in range(concurrency):
        _enqueue_committed(worker_session_factory, JobType.FETCH_ARTICLE)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)
    await worker.start()
    for _ in range(concurrency):
        await asyncio.wait_for(started.acquire(), timeout=2.0)

    # Act
    began = time.monotonic()
    await asyncio.wait_for(worker.stop(), timeout=5.0)
    elapsed = time.monotonic() - began

    # Assert: 直列なら 2 * 0.4 = 0.8 秒を超える。並行なら 0.4 秒台に収まる。
    assert elapsed < cancel_await_timeout * 2

    # ハンドラのバックグラウンド継続分が終わるのを待ってからテストを抜ける。
    await asyncio.sleep(1.1)


async def test_stop_logs_exceptions_from_tasks_that_finish_within_the_grace_period(
    worker_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM: 猶予内に例外で終わったタスクがあれば stop() がログに記録すること。

    `caplog` ではなく `techradar.jobs.worker.logger.error` を直接差し替えて検証する。
    `migrated_engine`（セッションスコープ）経由で alembic の `env.py` が呼ぶ
    `logging.config.fileConfig`（既定で `disable_existing_loggers=True`）により、
    このモジュールの logger インスタンスがセッション内で disabled になりうるため、
    `caplog` が記録を拾えないことがある。ロガーの有効/無効に依存しない検証にする。
    """
    # Arrange
    error_calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        worker_module.logger, "error", lambda *args, **_kwargs: error_calls.append(args)
    )
    registry = JobHandlerRegistry()
    settings = _fast_settings(worker_shutdown_grace_seconds=1.0)
    worker = JobWorker(settings=settings, registry=registry, session_factory=worker_session_factory)
    await worker.start()

    async def _boom() -> None:
        message = "worker loop exploded"
        raise RuntimeError(message)

    broken_task = asyncio.create_task(_boom())
    worker._tasks = [*worker._tasks, broken_task]

    # Act
    await worker.stop()

    # Assert
    assert any(
        call[0] == "worker.loop_task_raised_during_shutdown error=%s" for call in error_calls
    )
    assert any(
        isinstance(call[1], RuntimeError) and str(call[1]) == "worker loop exploded"
        for call in error_calls
    )
