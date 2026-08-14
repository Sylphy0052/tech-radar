"""公式ソースレジストリの管理 API（`PROJECT_SPEC.md` §11）。

自動判定は必ず外れる。誤判定を手で直せる経路を用意し、直した内容は
`verified` で「手動確認済み」と区別してシーダーの上書きから守る。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.db import SourceRegistry
from techradar.db.enums import SourceType
from techradar.db.errors import is_unique_violation
from techradar.db.query import escape_like_pattern

router = APIRouter(prefix="/api/sources", tags=["sources"])

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 100

SessionDep = Annotated[Session, Depends(get_session)]


class SourceResponse(BaseModel):
    """レジストリ 1 件のレスポンス。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_name: str
    domain: str
    path_pattern: str | None
    github_org: str | None
    source_type: str
    authority_score: float
    verified: bool


class SourceCreate(BaseModel):
    """レジストリの新規登録。"""

    model_config = ConfigDict(extra="forbid")

    entity_name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=255)
    path_pattern: str | None = Field(default=None, max_length=255)
    github_org: str | None = Field(default=None, max_length=100)
    source_type: SourceType
    authority_score: float = Field(ge=0.0, le=1.0)
    # 手で登録した行は確認済みとして扱い、シーダーに上書きさせない。
    verified: bool = True


class SourceUpdate(BaseModel):
    """レジストリの部分更新。

    省略した項目は変更しない。`domain`・`path_pattern`・`github_org` は一意制約
    `uq_source_registry_domain` を構成するキーであり、`service._rule_key` の
    同一性判定にも使われるため変更させない（別のパターンにしたい場合は新規登録
    する）。ここを変更可能にすると、PATCH で行のキーを変えた後にシーダーを
    走らせたとき、変更前のキーに一致する config 側の原ルールが「存在しない」と
    判定されて別行として再投入され、手直しした行と矛盾する形で併存してしまう。
    """

    model_config = ConfigDict(extra="forbid")

    entity_name: str | None = Field(default=None, min_length=1, max_length=200)
    source_type: SourceType | None = None
    authority_score: float | None = Field(default=None, ge=0.0, le=1.0)
    verified: bool | None = None


@router.get("", response_model=list[SourceResponse])
def list_sources(
    session: SessionDep,
    domain: Annotated[str | None, Query(description="ドメインの部分一致で絞り込む")] = None,
    entity_name: Annotated[
        str | None, Query(description="企業・OSS 名の部分一致で絞り込む")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SourceRegistry]:
    """登録済みの情報源を一覧する。"""
    statement = select(SourceRegistry).order_by(SourceRegistry.domain, SourceRegistry.path_pattern)
    if domain:
        statement = statement.where(
            SourceRegistry.domain.ilike(f"%{escape_like_pattern(domain)}%", escape="\\")
        )
    if entity_name:
        statement = statement.where(
            SourceRegistry.entity_name.ilike(f"%{escape_like_pattern(entity_name)}%", escape="\\")
        )
    return list(session.scalars(statement.limit(limit).offset(offset)).all())


@router.post("", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceCreate,
    session: SessionDep,
) -> SourceRegistry:
    """情報源を登録する。"""
    row = SourceRegistry(
        entity_name=payload.entity_name,
        domain=payload.domain,
        path_pattern=payload.path_pattern,
        github_org=payload.github_org,
        source_type=payload.source_type.value,
        authority_score=payload.authority_score,
        verified=payload.verified,
    )
    session.add(row)
    _flush_or_conflict(session)
    return row


@router.patch("/{source_id}", response_model=SourceResponse)
def update_source(
    source_id: uuid.UUID,
    payload: SourceUpdate,
    session: SessionDep,
) -> SourceRegistry:
    """情報源を部分更新する。

    更新した行は手動確認済みとして扱う（`verified` を明示指定した場合はそれに従う）。
    """
    row = session.get(SourceRegistry, source_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="情報源が見つかりません")

    updates = payload.model_dump(exclude_unset=True)
    if "source_type" in updates and updates["source_type"] is not None:
        updates["source_type"] = SourceType(updates["source_type"]).value
    for field, value in updates.items():
        setattr(row, field, value)
    if "verified" not in updates:
        row.verified = True

    _flush_or_conflict(session)
    return row


def _flush_or_conflict(session: Session) -> None:
    """flush し、一意制約違反だけを 409 に変換する。

    DB のエラーをそのまま 500 で返すと、利用者は原因（重複登録）が分からない。
    ただし一意制約違反以外（将来 FK や CHECK 制約が増えた場合など）まで
    一律 409 に丸めると、無関係なエラーに「重複登録」という誤ったメッセージを
    返してしまう。SQLSTATE で絞り込み、それ以外はロールバックしたうえで
    元の例外を再送出する（500 として扱わせる）。
    """
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if not is_unique_violation(exc):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="同じドメイン・パターンの情報源が既に登録されています",
        ) from exc
