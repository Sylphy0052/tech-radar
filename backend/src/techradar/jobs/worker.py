"""FastAPI プロセスに同居する asyncio ジョブワーカー（`PROJECT_SPEC.md` §6）。

キュー操作（`techradar.jobs.queue`）とハンドラ登録（`techradar.jobs.registry`）を
組み合わせて、実際にジョブを処理するループを提供する。DB 操作は同期 SQLAlchemy
のため、イベントループを塞がないよう必ず `asyncio.to_thread` 経由で呼び出す。
FastAPI への組み込み自体（`main.py` の変更）は後続タスクの担当。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job
from techradar.db.session import get_session_factory
from techradar.jobs import queue
from techradar.jobs.logging import record_job_event
from techradar.jobs.registry import JobContext, JobHandlerRegistry

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class JobWorker:
    """複数のワーカーコルーチンを束ねてジョブキューを処理する。"""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: JobHandlerRegistry,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._session_factory = session_factory or get_session_factory()
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        # worker_index -> 現在処理中のジョブ id。stop() が猶予超過タスクを
        # キャンセルしたあとに、どのジョブを release すべきか判定するために使う。
        self._current_jobs: dict[int, uuid.UUID] = {}

    async def start(self) -> None:
        """ワーカーコルーチンを起動する。

        起動時に一度 `reclaim_stale` を実行し、前回プロセスが強制終了された
        場合に実行中 status のまま残ったジョブを pending へ戻す。
        """
        reclaimed = await asyncio.to_thread(self._reclaim_stale_sync)
        logger.info("worker.reclaimed_stale count=%d", reclaimed)

        self._stop_event = asyncio.Event()
        self._current_jobs = {}
        self._tasks = [
            asyncio.create_task(self._run_worker_loop(index))
            for index in range(self._settings.worker_concurrency)
        ]

    async def stop(self) -> None:
        """猶予付きでワーカーを停止する。

        1. 停止フラグを立て、新規の claim を止める。
        2. 実行中のジョブが `worker_shutdown_grace_seconds` 以内に終わるのを待つ。
        3. 猶予を超えたタスクはキャンセルし、対象ジョブを `release` で pending へ
           戻す（利用者都合の中断のため attempts は増やさない）。
        """
        self._stop_event.set()
        if not self._tasks:
            return

        _done, pending = await asyncio.wait(
            self._tasks, timeout=self._settings.worker_shutdown_grace_seconds
        )
        for worker_index, task in enumerate(self._tasks):
            if task not in pending:
                continue
            job_id = self._current_jobs.get(worker_index)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if job_id is not None:
                await asyncio.to_thread(self._release_job_sync, job_id)
                logger.info("worker.released_on_shutdown job_id=%s", job_id)

        self._tasks = []
        self._current_jobs = {}

    async def _run_worker_loop(self, worker_index: int) -> None:
        """1本のワーカーコルーチンのループ本体。"""
        logger.info("worker.loop_started worker_index=%d", worker_index)
        while not self._stop_event.is_set():
            context = await asyncio.to_thread(self._claim_job_sync)
            if context is None:
                await self._wait_for_poll_interval_or_stop()
                continue

            self._current_jobs[worker_index] = context.job_id
            try:
                await self._process_job(context)
            finally:
                self._current_jobs.pop(worker_index, None)
        logger.info("worker.loop_stopped worker_index=%d", worker_index)

    async def _wait_for_poll_interval_or_stop(self) -> None:
        """pending が無いときの待機。停止要求が来ていれば即座に戻る。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._settings.worker_poll_interval_seconds,
            )

    async def _process_job(self, context: JobContext) -> None:
        """1件のジョブをハンドラへ渡し、結果に応じて完了・失敗を記録する。"""
        handler = self._registry.get(context.job_type)
        start = time.monotonic()

        if handler is None:
            error = f"未登録のジョブ種別です: {context.job_type.value}"
            logger.error(
                "job.unregistered_type job_id=%s job_type=%s",
                context.job_id,
                context.job_type.value,
            )
            await asyncio.to_thread(
                self._fail_job_sync, context.job_id, error, _elapsed_ms(start), retryable=False
            )
            return

        logger.info(
            "job.started job_id=%s job_type=%s attempts=%d",
            context.job_id,
            context.job_type.value,
            context.attempts,
        )
        try:
            await handler(context)
        except asyncio.CancelledError:
            # シャットダウンによるキャンセル。release は stop() 側の責務なので、
            # ここでは失敗として記録しない。
            raise
        except Exception as exc:
            duration_ms = _elapsed_ms(start)
            logger.exception(
                "job.failed job_id=%s job_type=%s", context.job_id, context.job_type.value
            )
            await asyncio.to_thread(
                self._fail_job_sync, context.job_id, str(exc), duration_ms, retryable=True
            )
            return

        duration_ms = _elapsed_ms(start)
        await asyncio.to_thread(self._complete_job_sync, context.job_id, duration_ms)
        logger.info(
            "job.completed job_id=%s job_type=%s duration_ms=%d",
            context.job_id,
            context.job_type.value,
            duration_ms,
        )

    # ---- 同期 DB 操作（必ず asyncio.to_thread 経由で呼ぶ） ----

    def _with_session(self, operation: Callable[[Session], _T]) -> _T:
        """セッションを開いて操作し、成功時のみ commit する。

        操作ごとに新しいセッションを開いて閉じる。トランザクションをコルーチンの
        生存期間より長く保持しないため（ジョブ処理中の待機で行ロックを握り続けない）。
        """
        session = self._session_factory()
        try:
            result = operation(session)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _reclaim_stale_sync(self) -> int:
        return self._with_session(queue.reclaim_stale)

    def _claim_job_sync(self) -> JobContext | None:
        def _operation(session: Session) -> JobContext | None:
            job = queue.claim_next(session)
            if job is None:
                return None
            return JobContext(
                job_id=job.id,
                job_type=JobType(job.type),
                payload=dict(job.payload),
                attempts=job.attempts,
            )

        return self._with_session(_operation)

    def _complete_job_sync(self, job_id: uuid.UUID, duration_ms: int) -> None:
        def _operation(session: Session) -> None:
            job = session.get(Job, job_id)
            if job is None:
                logger.warning("job.missing_on_complete job_id=%s", job_id)
                return
            queue.complete(session, job)
            record_job_event(
                session, job=job, status=JobStatus.COMPLETED.value, duration_ms=duration_ms
            )

        self._with_session(_operation)

    def _fail_job_sync(
        self, job_id: uuid.UUID, error: str, duration_ms: int, *, retryable: bool
    ) -> None:
        def _operation(session: Session) -> None:
            job = session.get(Job, job_id)
            if job is None:
                logger.warning("job.missing_on_fail job_id=%s", job_id)
                return
            queue.fail(
                session,
                job,
                error,
                max_attempts=self._settings.job_max_attempts,
                backoff_seconds=self._settings.job_retry_backoff_seconds,
                retryable=retryable,
            )
            # `queue.fail` はリトライ可能なら pending へ戻すため、log の status には
            # 更新後の実際の状態を入れる。常に failed と書くと、ログだけを見たときに
            # 再実行待ちのジョブが恒久的な失敗と区別できなくなる。
            record_job_event(
                session,
                job=job,
                status=job.status,
                duration_ms=duration_ms,
                error_reason=error,
            )

        self._with_session(_operation)

    def _release_job_sync(self, job_id: uuid.UUID) -> None:
        def _operation(session: Session) -> None:
            job = session.get(Job, job_id)
            if job is None:
                logger.warning("job.missing_on_release job_id=%s", job_id)
                return
            queue.release(session, job)

        self._with_session(_operation)


def _elapsed_ms(start: float) -> int:
    """`time.monotonic()` の差分をミリ秒の整数に変換する。"""
    return int((time.monotonic() - start) * 1000)
