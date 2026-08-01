"""ジョブの状態を operation_logs へ記録する（`PROJECT_SPEC.md` §24 可観測性）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from techradar.db.models import Job, OperationLog


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
    """
    log = OperationLog(
        operation=job.type,
        status=status,
        job_id=job.id,
        article_id=_extract_article_id(job.payload),
        duration_ms=duration_ms,
        error_reason=error_reason,
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
