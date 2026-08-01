"""巡回ジョブの起動 API（`PROJECT_SPEC.md` §20、Issue #8）。

常駐スケジューラを置かない設計のため、巡回は UI の実行ボタンから
`crawl_sources` ジョブを enqueue することで起動する。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.db.enums import JobType
from techradar.jobs.queue import enqueue

router = APIRouter(prefix="/api/crawl", tags=["crawl"])

SessionDep = Annotated[Session, Depends(get_session)]


class CrawlRunCreate(BaseModel):
    """巡回ジョブの起動リクエスト。省略可能。"""

    model_config = ConfigDict(extra="forbid")

    # 巡回の起点を絞りたい場合にのみ指定する。未指定なら通常どおり全件を対象にする。
    source_domain: str | None = Field(default=None, max_length=255)


class CrawlRunResponse(BaseModel):
    """巡回ジョブの起動結果。"""

    job_id: uuid.UUID
    status: str


@router.post("/runs", response_model=CrawlRunResponse, status_code=status.HTTP_201_CREATED)
def create_crawl_run(
    session: SessionDep,
    payload: CrawlRunCreate | None = None,
) -> CrawlRunResponse:
    """巡回ジョブを enqueue する。"""
    job_payload = {}
    if payload is not None and payload.source_domain is not None:
        job_payload["source_domain"] = payload.source_domain

    job = enqueue(session, JobType.CRAWL_SOURCES, job_payload)
    return CrawlRunResponse(job_id=job.id, status=job.status)
