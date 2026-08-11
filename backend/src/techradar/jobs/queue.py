"""ジョブキューの操作（`PROJECT_SPEC.md` §6）。

常駐スケジューラは置かない設計のため、ここではキューに対する登録・取得・完了・
失敗・中断復旧という「操作」のみを関数として提供する。ワーカーのループは
`techradar.jobs.worker` が担う。

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


def ownership_token(job: Job) -> datetime:
    """claim 済みジョブから所有権トークン（claim 時刻）を取り出す。

    `claim_next` は必ず `started_at` を設定するため、claim を経たジョブであれば
    None にはならない。None なら claim せずに完了・失敗を書こうとしており、
    呼び出し側の使い方が誤っている。所有権の判定を素通りさせないよう、
    ここで早期に失敗させる。
    """
    if job.started_at is None:
        message = "claim されていないジョブには所有権トークンがありません"
        raise ValueError(message)
    return job.started_at


def still_owns_job(job: Job, claimed_at: datetime) -> bool:
    """claim したときの所有権がまだ有効かを判定する。

    `claim_next` は `started_at` に claim 時刻を書き込む。`release` はこれを
    `None` に戻し、別のワーカーが再 claim すれば別の時刻に変わる。よって
    claim 時に控えた値との一致は「あのとき自分が取ったジョブのままか」を表す。

    status だけを見る判定では足りない。release されたジョブが別ワーカーに
    再 claim されると status は再び実行中へ戻るため、取り残された処理の
    書き込みが通ってしまう。
    """
    return job.started_at == claimed_at


def complete(session: Session, job: Job, *, claimed_at: datetime) -> bool:
    """ジョブを完了状態にする。

    claim したときの所有権が残っている場合のみ書き込む。ワーカーの `stop()` は
    猶予を超えたタスクをキャンセルして `release` するが、`asyncio.to_thread` が
    起こしたスレッド自体はキャンセルできず、後から完了を書きにくる。無条件に
    上書きすると、pending へ戻した（あるいは別ワーカーが再 claim した）ジョブを
    完了で踏みつぶし、二重実行と結果の取りこぼしを招く。

    Returns:
        実際に completed へ更新した場合は `True`。所有権を失っていて何もしな
        かった場合は `False`（呼び出し側がログへ反映できるようにするため）。
    """
    if not still_owns_job(job, claimed_at):
        return False

    job.status = JobStatus.COMPLETED.value
    job.finished_at = datetime.now(UTC)
    job.last_error = None
    session.flush()
    return True


def fail(
    session: Session,
    job: Job,
    error: str,
    *,
    max_attempts: int,
    backoff_seconds: float,
    retryable: bool = True,
    claimed_at: datetime,
) -> bool:
    """ジョブの失敗を記録し、リトライ可否に応じて状態を更新する。

    attempts は失敗のたびに増やす（claim 時ではなく）。プロセスが強制終了
    された場合、claim 時に増やしていると再起動後の `reclaim_stale` だけで
    リトライ回数を無駄に消費してしまうため。

    `complete` と同じく、claim したときの所有権が残っている場合のみ書き込む。
    中断されたジョブに後から失敗を記録すると、利用者都合の中断がリトライ回数を
    消費してしまう。

    Returns:
        実際に更新した場合は `True`。所有権を失っていて何もしなかった場合は
        `False`。
    """
    if not still_owns_job(job, claimed_at):
        return False

    job.attempts += 1
    job.last_error = error[:MAX_LAST_ERROR_LENGTH]

    if not retryable or job.attempts >= max_attempts:
        job.status = JobStatus.FAILED.value
        job.finished_at = datetime.now(UTC)
        session.flush()
        return True

    # まだリトライできる場合は pending に戻し、指数バックオフで
    # 再実行可能になる時刻を先に延ばす。
    job.status = JobStatus.PENDING.value
    job.started_at = None
    job.finished_at = None
    delay_seconds = backoff_seconds * (2 ** (job.attempts - 1))
    job.available_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    session.flush()
    return True


def release(session: Session, job: Job) -> bool:
    """シャットダウンなど利用者都合の中断でジョブを pending へ巻き戻す。

    実行中 status のときだけ pending に戻す条件付き更新にする。ワーカーの
    `stop()` は「猶予超過タスクをキャンセルして release する」処理を別スレッドの
    同期処理として呼ぶが、ハンドラが猶予境界ぎりぎりで終わり `complete` がスレッド内
    で先に走り切ることがある。無条件に pending へ上書きすると、completed になった
    直後のジョブを release が pending で踏みつぶし、二重実行を招くため、既に
    completed / failed / pending であれば何もしない。

    失敗ではないため attempts は増やさない。available_at も変更しない
    （中断は待機時間をリセットする理由にならないため）。

    Returns:
        実際に pending へ巻き戻した場合は `True`。対象外で何もしなかった場合は
        `False`（呼び出し側がログへ反映できるようにするため）。
    """
    if job.status not in {running_status.value for running_status in RUNNING_STATUSES}:
        return False

    job.status = JobStatus.PENDING.value
    job.started_at = None
    session.flush()
    return True


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
