"""巡回ジョブの起動 API（`PROJECT_SPEC.md` §20、Issue #8）。

常駐スケジューラを置かない設計のため、巡回は UI の実行ボタンから
`crawl_sources` ジョブを enqueue することで起動する。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job
from techradar.jobs.queue import enqueue
from techradar.jobs.status import RUNNING_STATUSES

router = APIRouter(prefix="/api/crawl", tags=["crawl"])

SessionDep = Annotated[Session, Depends(get_session)]

# ドメインとして妥当な文字種・構造のみ許可する（英数字・ハイフン・ドット区切りの
# 2ラベル以上、各ラベルは先頭末尾にハイフン不可）。SSRF 対策そのもの（内部アドレス
# の拒否等）は実際にアクセスする側の責務のため、ここでは形式検証に留める。
# pydantic-core（Rust の regex クレート）は look-around 未対応のため、
# 先頭末尾ハイフン不可は「英数字で始まり英数字で終わる」形の文字クラスで表現する。
_DOMAIN_LABEL_PATTERN = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_SOURCE_DOMAIN_PATTERN = rf"^{_DOMAIN_LABEL_PATTERN}(\.{_DOMAIN_LABEL_PATTERN})+$"

# crawl_sources ジョブが既に積まれている／実行中とみなす status。
_ACTIVE_CRAWL_JOB_STATUSES = frozenset(
    {JobStatus.PENDING.value, *(running_status.value for running_status in RUNNING_STATUSES)}
)


class CrawlRunCreate(BaseModel):
    """巡回ジョブの起動リクエスト。省略可能。"""

    model_config = ConfigDict(extra="forbid")

    # 巡回の起点を絞りたい場合にのみ指定する。未指定なら通常どおり全件を対象にする。
    source_domain: str | None = Field(default=None, max_length=255, pattern=_SOURCE_DOMAIN_PATTERN)


class CrawlRunResponse(BaseModel):
    """巡回ジョブの起動結果。"""

    job_id: uuid.UUID
    status: str


def _find_active_crawl_job(session: Session) -> Job | None:
    """pending または実行中 status の crawl_sources ジョブを1件探す。

    複数見つかりうる状況（reclaim_stale 直後など）でも、最も古いものを返せば
    UI 側の「進行中の巡回を追跡する」目的には十分なため、先着1件で良い。
    """
    stmt = (
        select(Job)
        .where(
            Job.type == JobType.CRAWL_SOURCES.value,
            Job.status.in_(_ACTIVE_CRAWL_JOB_STATUSES),
        )
        .order_by(Job.created_at)
        .limit(1)
    )
    return session.scalar(stmt)


@router.post(
    "/runs",
    response_model=CrawlRunResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": CrawlRunResponse,
            "description": (
                "pending または実行中の crawl_sources ジョブが既に存在するため、"
                "新規作成せず既存ジョブを返した。"
            ),
        },
    },
)
def create_crawl_run(
    session: SessionDep,
    response: Response,
    payload: CrawlRunCreate | None = None,
) -> CrawlRunResponse:
    """巡回ジョブを enqueue する。

    UI のボタン連打などで重複起動されると、`crawl_sources` 1件ごとに発生する
    検索 API / LLM 呼び出しが無制限に積み上がり、追加課金ゼロという前提に
    直接影響する。pending または実行中 status の `crawl_sources` ジョブが既に
    存在する場合は新規作成せず、その job_id を 200 OK で返す。存在しなければ
    従来どおり新規に enqueue して 201 Created を返す。
    """
    active_job = _find_active_crawl_job(session)
    if active_job is not None:
        response.status_code = status.HTTP_200_OK
        return CrawlRunResponse(job_id=active_job.id, status=active_job.status)

    job_payload = {}
    if payload is not None and payload.source_domain is not None:
        job_payload["source_domain"] = payload.source_domain

    job = enqueue(session, JobType.CRAWL_SOURCES, job_payload)
    return CrawlRunResponse(job_id=job.id, status=job.status)
