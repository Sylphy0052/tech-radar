"""FastAPI プロセスに同居する asyncio ジョブワーカー（`PROJECT_SPEC.md` §6）。

キュー操作（`techradar.jobs.queue`）とハンドラ登録（`techradar.jobs.registry`）を
組み合わせて、実際にジョブを処理するループを提供する。DB 操作は同期 SQLAlchemy
のため、イベントループを塞がないよう必ず `asyncio.to_thread` 経由で呼び出す。
FastAPI への組み込み（起動・停止）は `techradar.main` の lifespan が担う。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class _ClaimedJob:
    """claim したジョブと、その所有権を表すトークン。

    `claimed_at` はハンドラには渡さない。ハンドラが知る必要のない、ワーカーと
    キュー層の間だけの取り決めのため、`JobContext` とは分けて持つ。
    """

    context: JobContext
    claimed_at: datetime


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
        # キャンセルを無視して生き残ったタスク。参照を保持しないと実行途中で
        # GC されうるため、プロセスが終わるまで手放さない。
        self._abandoned_tasks: list[asyncio.Task[None]] = []

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
           この間に例外で終わったタスクがあればログに残す（`CancelledError` は
           シャットダウン起因であり得るため除く）。
        3. 猶予を超えたタスクはキャンセルし、`worker_cancel_await_timeout_seconds`
           を上限に終了を待つ。ハンドラが `CancelledError` を握り潰す実装だと
           無期限にブロックしうるため、ここでも待ち時間に上限を設ける。
        4. キャンセルできたタスクの対象ジョブは `release` で pending へ戻す
           （利用者都合の中断のため attempts は増やさない）。ただし `release` は
           実行中 status のときだけ巻き戻す条件付き更新のため、猶予内にスレッド側で
           先に completed / failed へ進んでいた場合は何もしない。
        5. 最後に `reclaim_stale` を実行する。claim 直後にキャンセルされて
           `_current_jobs` に登録される前にコルーチンが死んだジョブは、この worker
           からは追跡できない（実行中 status のまま宙に浮く）ため、まとめて回収する。

        3〜4 は全タスクぶんを並行して行う。1件ずつ順番に待つと、同時に猶予を
        超えたタスクの数だけ `worker_cancel_await_timeout_seconds` が積み上がり、
        シャットダウンが `worker_concurrency` に比例して伸びるため。

        1件の release が失敗しても、残りの release・取り残しタスクの退避・
        `reclaim_stale` は必ず実行する。停止処理は「どれか1つが転んだら全部やめる」
        より「できるところまでやって、失敗はログに残す」ほうが被害が小さい。
        """
        self._stop_event.set()
        # 前回の stop() 以降に終わったタスクを落とす。参照を持ち続ける必要が
        # あるのは、まだ動いているものだけ。
        self._abandoned_tasks = [task for task in self._abandoned_tasks if not task.done()]

        if self._tasks:
            done, pending = await asyncio.wait(
                self._tasks, timeout=self._settings.worker_shutdown_grace_seconds
            )
            self._log_task_exceptions(done)

            # 対象ジョブはキャンセル前に控える。キャンセルされたタスク自身が
            # `finally` で `_current_jobs` から自分を消すため、キャンセル後に
            # 引くと release すべきジョブを見失いうる。
            overdue = [
                (worker_index, task, self._current_jobs.get(worker_index))
                for worker_index, task in enumerate(self._tasks)
                if task in pending
            ]
            for _worker_index, task, _job_id in overdue:
                task.cancel()
            results = await asyncio.gather(
                *(
                    self._cancel_and_release(worker_index, task, job_id)
                    for worker_index, task, job_id in overdue
                ),
                return_exceptions=True,
            )
            for (worker_index, _task, job_id), result in zip(overdue, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(
                        "worker.release_on_shutdown_failed worker_index=%d job_id=%s error=%s",
                        worker_index,
                        job_id,
                        result,
                        exc_info=result,
                    )

            # 待ち切れなかったタスクは、キャンセルを無視して生き続けている可能性が
            # ある。参照を捨てるとイベントループ上で実行中のまま GC の対象になり、
            # どこで終わったのかも追えなくなるため、プロセスが終わるまで保持する。
            self._abandoned_tasks.extend(
                task for _index, task, _job_id in overdue if not task.done()
            )
            self._tasks = []
            self._current_jobs = {}

        reclaimed = await asyncio.to_thread(self._reclaim_stale_sync)
        logger.info("worker.reclaimed_stale_on_shutdown count=%d", reclaimed)

    async def _cancel_and_release(
        self, worker_index: int, task: asyncio.Task[None], job_id: uuid.UUID | None
    ) -> None:
        """キャンセル済みタスクの終了を待ち、対象ジョブを pending へ戻す。"""
        await self._await_cancelled_task(task, worker_index=worker_index, job_id=job_id)
        if job_id is not None:
            await self._release_job(job_id)

    def _log_task_exceptions(self, done_tasks: set[asyncio.Task[None]]) -> None:
        """猶予内に終了したタスクのうち、例外で終わったものをログに残す。

        `asyncio.wait` は `done` 側のタスクの成否を判定しないため、ここで明示的に
        確認しない限り、ワーカーが例外で死んでいても気付けない。
        """
        for task in done_tasks:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is None:
                continue
            logger.error("worker.loop_task_raised_during_shutdown error=%s", exc, exc_info=exc)

    async def _await_cancelled_task(
        self, task: asyncio.Task[None], *, worker_index: int, job_id: uuid.UUID | None
    ) -> None:
        """`task.cancel()` 後の終了を、ハードタイムアウト付きで待つ。

        `await task` を直接使うと、ハンドラが `CancelledError` を握り潰して
        処理を続ける実装の場合に無期限へブロックする（`asyncio.wait_for` も
        対象タスク自身が完了しない限り同様に待ち続けるため使えない）。
        `asyncio.wait` はタイムアウトで必ず戻るため、ここではこちらを使う。
        """
        _done, still_pending = await asyncio.wait(
            [task], timeout=self._settings.worker_cancel_await_timeout_seconds
        )
        if task in still_pending:
            logger.error(
                "worker.cancel_await_timed_out worker_index=%d job_id=%s", worker_index, job_id
            )

    async def _release_job(self, job_id: uuid.UUID) -> None:
        """猶予超過でキャンセルしたジョブを release し、結果をログに残す。"""
        released = await asyncio.to_thread(self._release_job_sync, job_id)
        if released:
            logger.info("worker.released_on_shutdown job_id=%s", job_id)
        else:
            logger.info("worker.release_skipped_already_finished job_id=%s", job_id)

    async def _run_worker_loop(self, worker_index: int) -> None:
        """1本のワーカーコルーチンのループ本体。

        `_claim_job_sync` / `_complete_job_sync` / `_fail_job_sync` は DB 接続断
        などで想定外の例外を投げうる。ここで捕まえずコルーチンの外へ伝播させると
        ワーカーが1本死んで戻らず、`reclaim_stale` は起動時にしか走らないため
        プロセスを再起動するまで concurrency が恒久的に減ったままになる。
        `asyncio.CancelledError` はシャットダウンの経路のため素通しし、それ以外の
        `Exception` はログに残してループを継続する。
        """
        logger.info("worker.loop_started worker_index=%d", worker_index)
        while not self._stop_event.is_set():
            try:
                claimed = await asyncio.to_thread(self._claim_job_sync)
                if claimed is None:
                    await self._wait_for_poll_interval_or_stop()
                    continue

                self._current_jobs[worker_index] = claimed.context.job_id
                try:
                    await self._process_job(claimed)
                finally:
                    self._current_jobs.pop(worker_index, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 暴走ループを避けるため、poll interval だけ待ってから次の周回へ進む。
                logger.exception("worker.loop_iteration_failed worker_index=%d", worker_index)
                await self._wait_for_poll_interval_or_stop()
        logger.info("worker.loop_stopped worker_index=%d", worker_index)

    async def _wait_for_poll_interval_or_stop(self) -> None:
        """pending が無いときの待機。停止要求が来ていれば即座に戻る。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self._settings.worker_poll_interval_seconds,
            )

    async def _process_job(self, claimed: _ClaimedJob) -> None:
        """1件のジョブをハンドラへ渡し、結果に応じて完了・失敗を記録する。"""
        context = claimed.context
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
                self._fail_job_sync,
                claimed,
                error,
                _elapsed_ms(start),
                retryable=False,
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
                self._fail_job_sync, claimed, str(exc), duration_ms, retryable=True
            )
            return

        duration_ms = _elapsed_ms(start)
        await asyncio.to_thread(self._complete_job_sync, claimed, duration_ms)
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

    def _claim_job_sync(self) -> _ClaimedJob | None:
        def _operation(session: Session) -> _ClaimedJob | None:
            job = queue.claim_next(session)
            if job is None:
                return None
            return _ClaimedJob(
                context=JobContext(
                    job_id=job.id,
                    job_type=JobType(job.type),
                    payload=dict(job.payload),
                    attempts=job.attempts,
                ),
                claimed_at=queue.ownership_token(job),
            )

        return self._with_session(_operation)

    def _complete_job_sync(self, claimed: _ClaimedJob, duration_ms: int) -> None:
        job_id = claimed.context.job_id

        def _operation(session: Session) -> None:
            # 行ロックを取ってから所有権を判定する。ロックなしに読むと、判定と更新の
            # 間に別セッションの release がコミットされ、古い started_at を見た
            # まま無条件で上書きしてしまう。
            job = session.get(Job, job_id, with_for_update=True)
            if job is None:
                logger.warning("job.missing_on_complete job_id=%s", job_id)
                return
            if not queue.complete(session, job, claimed_at=claimed.claimed_at):
                # 猶予超過でキャンセルされた後も生き残っていたスレッドが、
                # release 済み（あるいは再 claim 済み）のジョブを書きにきた。
                # 完了ログも残さない。このワーカーの処理結果は既に無効なため。
                logger.warning("job.complete_skipped_not_owner job_id=%s", job_id)
                return
            record_job_event(
                session, job=job, status=JobStatus.COMPLETED.value, duration_ms=duration_ms
            )

        self._with_session(_operation)

    def _fail_job_sync(
        self, claimed: _ClaimedJob, error: str, duration_ms: int, *, retryable: bool
    ) -> None:
        job_id = claimed.context.job_id

        def _operation(session: Session) -> None:
            # 行ロックを取ってから所有権を判定する。ロックなしに読むと、判定と更新の
            # 間に別セッションの release がコミットされ、古い started_at を見た
            # まま無条件で上書きしてしまう。
            job = session.get(Job, job_id, with_for_update=True)
            if job is None:
                logger.warning("job.missing_on_fail job_id=%s", job_id)
                return
            written = queue.fail(
                session,
                job,
                error,
                max_attempts=self._settings.job_max_attempts,
                backoff_seconds=self._settings.job_retry_backoff_seconds,
                retryable=retryable,
                claimed_at=claimed.claimed_at,
            )
            if not written:
                logger.warning("job.fail_skipped_not_owner job_id=%s", job_id)
                return
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

    def _release_job_sync(self, job_id: uuid.UUID) -> bool:
        def _operation(session: Session) -> bool:
            # 行ロックを取ってから所有権を判定する。ロックなしに読むと、判定と更新の
            # 間に別セッションの release がコミットされ、古い started_at を見た
            # まま無条件で上書きしてしまう。
            job = session.get(Job, job_id, with_for_update=True)
            if job is None:
                logger.warning("job.missing_on_release job_id=%s", job_id)
                return False
            return queue.release(session, job)

        return self._with_session(_operation)


def _elapsed_ms(start: float) -> int:
    """`time.monotonic()` の差分をミリ秒の整数に変換する。"""
    return int((time.monotonic() - start) * 1000)
