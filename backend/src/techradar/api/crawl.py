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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.db.enums import JobStatus, JobType
from techradar.db.errors import is_unique_violation
from techradar.db.models import Job
from techradar.jobs.queue import enqueue
from techradar.jobs.status import running_status_for

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
# crawl_sources が取りうる実行中 status は searching だけのため、他のジョブ種別の
# 実行中 status まで含めない（部分ユニークインデックス ux_jobs_active_crawl_sources の
# 述語と一致させ、両者がずれないようにする）。
_ACTIVE_CRAWL_JOB_STATUSES = frozenset(
    {JobStatus.PENDING.value, running_status_for(JobType.CRAWL_SOURCES).value}
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

    事前確認と INSERT の間には別リクエストが割り込みうる（TOCTOU）。SELECT 時点で
    対象の行が無い以上、行ロックでは塞げないため、最終的な排他は部分ユニーク
    インデックス `ux_jobs_active_crawl_sources` に任せ、ここではその違反を
    「相手が先に積んだ」ものとして扱う。
    """
    active_job = _find_active_crawl_job(session)
    if active_job is not None:
        response.status_code = status.HTTP_200_OK
        return CrawlRunResponse(job_id=active_job.id, status=active_job.status)

    job_payload = {}
    if payload is not None and payload.source_domain is not None:
        job_payload["source_domain"] = payload.source_domain

    try:
        job = enqueue(session, JobType.CRAWL_SOURCES, job_payload)
    except IntegrityError as exc:
        # 判定より先に巻き戻す。以降の問い合わせを失敗したトランザクション上で
        # 実行させないため、また再送出する場合も呼び出し側へ中断状態の
        # セッションを渡さないため（`api/sources.py` の `_flush_or_conflict` と同じ順序）。
        session.rollback()
        if not is_unique_violation(exc):
            raise
        return _existing_job_response(session, response, exc)

    # 応答を返す前にコミットする。リクエスト単位のセッションは依存の後処理で
    # コミットされるが、後処理が走るのはレスポンス送信より後になる。UI は応答直後に
    # `GET /api/jobs/{job_id}` でポーリングを始めるため、ここでコミットしておかないと
    # 起動したばかりのジョブが 404 になる。
    session.commit()

    return CrawlRunResponse(job_id=job.id, status=job.status)


def _existing_job_response(
    session: Session, response: Response, exc: IntegrityError
) -> CrawlRunResponse:
    """一意制約で弾かれたとき、競合相手が積んだジョブを 200 OK として返す。

    呼び出し側でロールバック済みであることを前提に、改めてアクティブなジョブを
    引き直す。相手のジョブが引き直しまでの間に完了していると返すべきジョブが
    無くなるため、その場合は握り潰さず元の例外を送出する（重複ではなく想定外の
    状態のため）。
    """
    active_job = _find_active_crawl_job(session)
    if active_job is None:
        raise exc

    response.status_code = status.HTTP_200_OK
    return CrawlRunResponse(job_id=active_job.id, status=active_job.status)
