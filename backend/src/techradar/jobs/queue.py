"""ジョブキューの操作（`PROJECT_SPEC.md` §6）。

常駐スケジューラは置かない設計のため、ここではキューに対する登録・取得・完了・
失敗・中断復旧という「操作」のみを関数として提供する。ワーカーのループや
プロセス管理は後続タスク（T3）の責務であり、ここには含めない。

すべて同期 SQLAlchemy Session を引数に取り、コミットは呼び出し側に委ねる
（`flush` までで止める）。トランザクション境界の決定はキュー操作自体の責務
ではなく、呼び出し側のユースケース次第のため。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job
from techradar.jobs.status import RUNNING_STATUSES, running_status_for

# last_error が際限なく伸びて jobs テーブルを肥大化させないための上限。
MAX_LAST_ERROR_LENGTH = 2000


def enqueue(session: Session, job_type: JobType, payload: dict[str, Any] | None = None) -> Job:
    """新しいジョブを pending 状態でキューに積む。

    `available_at` は DB 既定（`now()`）のままにする。積んだ直後から
    実行してよいジョブだから、ここで明示的に値を入れる理由がない。
    """
    job = Job(type=job_type.value, payload=payload or {}, status=JobStatus.PENDING.value)
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session, *, now: datetime | None = None) -> Job | None:
    """次に実行可能なジョブを1件取得し、実行中 status へ遷移させる。

    `FOR UPDATE SKIP LOCKED` で行ロックする。複数ワーカーが同時に呼んでも、
    ロック中の行は他の呼び出しから見えなくなるため、同一ジョブを2重に
    取得することがない（受入基準）。
    """
    claim_time = now or datetime.now(UTC)
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.PENDING.value, Job.available_at <= claim_time)
        .order_by(Job.available_at, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None

    # attempts はここでは増やさない。プロセスが強制終了された場合に
    # リトライ回数を無駄に消費しないようにするため（試行回数は失敗時に数える）。
    job.status = running_status_for(JobType(job.type)).value
    job.started_at = claim_time
    session.flush()
    return job


def complete(session: Session, job: Job) -> None:
    """ジョブを完了状態にする。"""
    job.status = JobStatus.COMPLETED.value
    job.finished_at = datetime.now(UTC)
    job.last_error = None
    session.flush()


def fail(
    session: Session,
    job: Job,
    error: str,
    *,
    max_attempts: int,
    backoff_seconds: float,
    retryable: bool = True,
) -> None:
    """ジョブの失敗を記録し、リトライ可否に応じて状態を更新する。

    attempts は失敗のたびに増やす（claim 時ではなく）。プロセスが強制終了
    された場合、claim 時に増やしていると再起動後の `reclaim_stale` だけで
    リトライ回数を無駄に消費してしまうため。
    """
    job.attempts += 1
    job.last_error = error[:MAX_LAST_ERROR_LENGTH]

    if not retryable or job.attempts >= max_attempts:
        job.status = JobStatus.FAILED.value
        job.finished_at = datetime.now(UTC)
        session.flush()
        return

    # まだリトライできる場合は pending に戻し、指数バックオフで
    # 再実行可能になる時刻を先に延ばす。
    job.status = JobStatus.PENDING.value
    job.started_at = None
    job.finished_at = None
    delay_seconds = backoff_seconds * (2 ** (job.attempts - 1))
    job.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    session.flush()


def release(session: Session, job: Job) -> None:
    """シャットダウンなど利用者都合の中断でジョブを pending へ巻き戻す。

    失敗ではないため attempts は増やさない。available_at も変更しない
    （中断は待機時間をリセットする理由にならないため）。
    """
    job.status = JobStatus.PENDING.value
    job.started_at = None
    session.flush()


def reclaim_stale(session: Session) -> int:
    """実行中 status のまま残っているジョブを一括で pending に戻す。

    プロセスが強制終了されると行ロックは接続の切断とともに解放されるが、
    status 自体は実行中のまま DB に残ってしまう。起動時にこれを検出して
    復旧するために使う。対象件数が多くなりうるため、1行ずつ ORM で
    更新せず UPDATE 文1本で処理する。
    """
    stmt = (
        update(Job)
        .where(Job.status.in_([status.value for status in RUNNING_STATUSES]))
        .values(status=JobStatus.PENDING.value, started_at=None)
    )
    # Session.execute() の戻り値型は Result[Any] で rowcount を持たない。
    # Connection.execute() は常に CursorResult を返す（rowcount を持つ）ため、
    # セッションの現在のトランザクションに紐づく接続を明示的に使う。
    result = session.connection().execute(stmt)
    session.flush()
    return result.rowcount
