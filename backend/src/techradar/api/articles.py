"""URL 登録 API（`PROJECT_SPEC.md` §6.2, §20, Issue #12）。

MVP は単一ユーザー・認証なし（§22）のため、登録者は常に
`api.deps.get_current_user_id` が返す ID を使う。認証を導入する際は
その依存の実装を差し替えればよい。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_current_user_id, get_session
from techradar.db.enums import JobStatus, JobType
from techradar.db.errors import is_unique_violation
from techradar.db.models import ArticleRegistration
from techradar.fetcher.url import normalize_url
from techradar.jobs.queue import enqueue

router = APIRouter(prefix="/api/articles", tags=["articles"])

SessionDep = Annotated[Session, Depends(get_session)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]

# 極端に長い URL による無用なリソース消費を避けるための上限。
MAX_URL_LENGTH = 2048
_ALLOWED_URL_SCHEMES = ("http://", "https://")


class ArticleRegistrationCreate(BaseModel):
    """URL 登録リクエスト。

    SSRF 対策そのもの（内部アドレスの拒否等）は実際に取得する
    `techradar.fetcher` 側の責務のため、ここではスキームと長さの形式検証に
    留める（`crawl.py` の `CrawlRunCreate` と同じ方針）。
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)

    @field_validator("url")
    @classmethod
    def _validate_scheme(cls, value: str) -> str:
        if not value.startswith(_ALLOWED_URL_SCHEMES):
            message = "http または https で始まる URL のみ登録できます"
            raise ValueError(message)
        return value


class ArticleRegistrationResponse(BaseModel):
    """登録の公開表現。

    `normalized_url` と `user_id` は含めない。内部の正規化結果やユーザー識別子を
    無用に露出させないため（`jobs.py` の `JobResponse` と同じ方針）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    status: str
    article_id: uuid.UUID | None
    error_reason: str | None
    created_at: datetime
    updated_at: datetime


def _find_existing_registration(
    session: Session, user_id: uuid.UUID, normalized_url: str
) -> ArticleRegistration | None:
    """同一ユーザー・同一正規化 URL の既存登録を探す。"""
    statement = select(ArticleRegistration).where(
        ArticleRegistration.user_id == user_id,
        ArticleRegistration.normalized_url == normalized_url,
    )
    return session.scalar(statement)


def _create_registration(
    session: Session, user_id: uuid.UUID, url: str, normalized_url: str
) -> ArticleRegistration | None:
    """新規登録行を作る。

    同時リクエストによる TOCTOU で一意制約に違反した場合は None を返し、
    呼び出し側に既存行の再取得を委ねる。
    """
    registration = ArticleRegistration(
        user_id=user_id,
        url=url,
        normalized_url=normalized_url,
        status=JobStatus.PENDING.value,
    )
    session.add(registration)
    try:
        session.flush()
    except IntegrityError as exc:
        # 判定より先に巻き戻す（`crawl.py` の `create_crawl_run` と同じ順序）。
        session.rollback()
        if not is_unique_violation(exc):
            # 一意制約以外の整合性エラーを「重複登録」として握り潰すと、
            # 原因の異なる失敗が 500 ではなく不可解な応答として現れる。
            raise
        return None
    return registration


@router.post(
    "",
    response_model=ArticleRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": ArticleRegistrationResponse,
            "description": "同一 URL の登録が既に存在するため、新規作成せず既存の登録を返した。",
        },
    },
)
def create_article_registration(
    payload: ArticleRegistrationCreate,
    session: SessionDep,
    user_id: UserIdDep,
    response: Response,
) -> ArticleRegistration:
    """URL を登録し、記事取得ジョブを積む。

    同じ URL（正規化後）を何度も登録しても fetch ジョブを積み増さないよう、
    既存登録があればそれをそのまま返す。
    """
    normalized_url = normalize_url(payload.url)

    existing = _find_existing_registration(session, user_id, normalized_url)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    registration = _create_registration(session, user_id, payload.url, normalized_url)
    if registration is None:
        # 同時リクエストで既に他方が登録済み（TOCTOU）。作成はせず既存を返す。
        response.status_code = status.HTTP_200_OK
        conflicting = _find_existing_registration(session, user_id, normalized_url)
        if conflicting is None:
            # 直前に一意制約違反しているため、理論上ここには来ない。
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="登録の重複解決に失敗しました",
            )
        return conflicting

    job = enqueue(
        session,
        JobType.FETCH_ARTICLE,
        {"registration_id": str(registration.id), "url": payload.url},
    )
    registration.job_id = job.id
    # 応答を返す前にコミットする。リクエスト単位のセッションは依存の後処理で
    # コミットされるが、後処理が走るのはレスポンス送信より後になる。UI は応答直後に
    # `GET /api/articles/registrations/{id}` でポーリングを始めるため、ここで
    # コミットしておかないと登録直後の状態取得が 404 になる。また、fetch_article を
    # 拾うワーカーにとってもジョブが見えるようになるのが応答後になる。
    session.commit()
    return registration


@router.get("/registrations/{registration_id}", response_model=ArticleRegistrationResponse)
def get_article_registration(
    registration_id: uuid.UUID,
    session: SessionDep,
    user_id: UserIdDep,
) -> ArticleRegistration:
    """登録の状態を取得する。

    作成側と同じく `user_id` で絞る。絞らないままにすると、認証を導入した際に
    このエンドポイントだけ他ユーザーの登録を返してしまう。
    """
    registration = session.scalar(
        select(ArticleRegistration).where(
            ArticleRegistration.id == registration_id,
            ArticleRegistration.user_id == user_id,
        )
    )
    if registration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="登録が見つかりません")
    return registration
