"""ジョブキュー層（`PROJECT_SPEC.md` §6）。

登録・取得・完了・失敗・中断復旧のキュー操作に加え、ハンドラ登録機構
（`registry`）と asyncio ワーカー（`worker`）を提供する。
"""

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
from techradar.jobs.registry import (
    JobContext,
    JobHandler,
    JobHandlerRegistry,
    create_default_registry,
)
from techradar.jobs.status import RUNNING_STATUSES, running_status_for
from techradar.jobs.worker import JobWorker

__all__ = [
    "MAX_LAST_ERROR_LENGTH",
    "RUNNING_STATUSES",
    "JobContext",
    "JobHandler",
    "JobHandlerRegistry",
    "JobWorker",
    "claim_next",
    "complete",
    "create_default_registry",
    "enqueue",
    "fail",
    "reclaim_stale",
    "record_job_event",
    "release",
    "running_status_for",
]
