"""ジョブの状態を operation_logs へ記録する（`PROJECT_SPEC.md` §24 可観測性）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from techradar.db.models import Job, OperationLog
from techradar.jobs.queue import MAX_LAST_ERROR_LENGTH


def record_job_event(
    session: Session,
    *,
    job: Job,
    status: str,
    duration_ms: int | None = None,
    error_reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> OperationLog:
    """ジョブの実行結果を1行の operation_log として記録する。

    payload に article_id が含まれていれば article_id 列にも複製する。
    記事単位でログを絞り込めるようにするため。UUID として解釈できない値は
    無視する（ジョブ種別によっては article_id を持たない payload もあり、
    ログ記録自体を失敗させる理由にはならないため）。

    `error_reason` は `jobs.last_error` と同じ `MAX_LAST_ERROR_LENGTH` で
    切り詰める。同じ例外メッセージ由来の値が片方だけ無制限に伸びると、
    上限を設けた意図（テーブル肥大化の防止）が一貫しなくなるため。
    """
    log = OperationLog(
        operation=job.type,
        status=status,
        job_id=job.id,
        article_id=_extract_article_id(job.payload),
        duration_ms=duration_ms,
        error_reason=error_reason[:MAX_LAST_ERROR_LENGTH] if error_reason is not None else None,
        details=details or {},
    )
    session.add(log)
    session.flush()
    return log


def _extract_article_id(payload: dict[str, Any]) -> uuid.UUID | None:
    """payload の article_id を UUID として取り出す。解釈できなければ None を返す。"""
    raw = payload.get("article_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None
