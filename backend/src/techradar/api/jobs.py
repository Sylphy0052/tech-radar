"""ジョブの進捗取得 API（`PROJECT_SPEC.md` §20）。

常駐スケジューラを置かない設計のため、UI 側はジョブを enqueue したあと
このエンドポイントをポーリングして進捗を確認する。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.db.models import Job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

SessionDep = Annotated[Session, Depends(get_session)]


class JobResponse(BaseModel):
    """ジョブ 1 件の進捗レスポンス。

    `payload` は含めない。ジョブの投入内容には将来 URL など内部情報が
    入りうるため、進捗確認 API で無条件に露出させない。

    `last_error` も同様の理由で含めない。失敗理由は例外メッセージそのものであり、
    今後 fetch_article などが外部 HTTP を叩くハンドラを持つと、アクセス先 URL や
    クエリパラメータ（API キーを含みうる）がそのまま入りうる。この進捗確認 API は
    無認証で叩けるため、ここで露出させると内部情報の漏洩経路になる。失敗理由は
    `operation_logs.error_reason`（非公開経路）に別途記録される。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    status: str
    attempts: int
    created_at: datetime
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: uuid.UUID, session: SessionDep) -> Job:
    """ジョブの進捗を取得する。"""
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ジョブが見つかりません")
    return job
