"""`purge_operation_logs` ジョブハンドラ（`PROJECT_SPEC.md` §24 可観測性、Issue #19）。

`operation_logs` の保持期間は 90 日（`docs/decisions.md`）だが、Issue #2 で
テーブルとインデックスを作った時点では実際に削除する実行主体が無かった。
常駐スケジューラを置かない設計のため、`crawl_sources` の完了時に積まれる
ジョブとしてこの削除を担う。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db.models import OperationLog
from techradar.jobs.handlers._shared import run_job_in_thread
from techradar.jobs.registry import JobContext, JobHandler

logger = logging.getLogger(__name__)


def purge_expired_operation_logs(
    session: Session, *, retention_days: int, now: datetime | None = None
) -> int:
    """保持期間を超えた `operation_logs` を削除し、削除件数を返す。

    境界は `created_at < cutoff` にする。ちょうど cutoff の行は「保持期間を
    超えた」とは言えないため残す。

    対象件数は運用期間に比例して増えうるため、ORM で1行ずつ削除せず DELETE 文
    1本で処理する（`techradar.jobs.queue.reclaim_stale` と同じ方針）。
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    stmt = delete(OperationLog).where(OperationLog.created_at < cutoff)
    # Session.execute() の戻り値型は Result[Any] で rowcount を持たない。
    # Connection.execute() は常に CursorResult を返すため、セッションの現在の
    # トランザクションに紐づく接続を明示的に使う（`reclaim_stale` と同じ）。
    result = session.connection().execute(stmt)
    session.flush()
    # 削除で消えた行を ORM が古いまま参照し続けないよう、Identity Map を捨てる。
    session.expire_all()
    return result.rowcount


def process_purge_operation_logs(session: Session, context: JobContext, settings: Settings) -> None:
    """`purge_operation_logs` ジョブ 1 件分の処理。

    保持日数は `settings.log_retention_days`（既定 90）に従う。ジョブの payload
    では上書きしない。保持期間はプロジェクト全体の方針であり、個々のジョブが
    任意の値を持ち込めると、積まれた経路によって削除範囲が変わってしまうため。
    """
    deleted = purge_expired_operation_logs(session, retention_days=settings.log_retention_days)
    logger.info(
        "purge_operation_logs.deleted job_id=%s count=%s retention_days=%s",
        context.job_id,
        deleted,
        settings.log_retention_days,
    )


def make_purge_operation_logs_handler(settings: Settings | None = None) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `purge_operation_logs` ハンドラを作る。"""
    resolved_settings = settings or get_settings()

    async def _handle(context: JobContext) -> None:
        await run_job_in_thread(context, resolved_settings, process_purge_operation_logs)

    return _handle
