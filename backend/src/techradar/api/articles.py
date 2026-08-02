"""URL 登録 API（`PROJECT_SPEC.md` §6.2, §20, Issue #12）。

MVP は単一ユーザー・認証なし（§22）のため、登録者は常に
`api.deps.get_current_user_id` が返す ID を使う。認証を導入する際は
その依存の実装を差し替えればよい。
"""

from __future__ import annotations

import base64
import binascii
import math
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_current_user_id, get_session
from techradar.db.enums import ArticleOrigin, JobStatus, JobType
from techradar.db.errors import is_unique_violation
from techradar.db.models import Article, ArticleRegistration, UserArticle
from techradar.fetcher.url import normalize_url
from techradar.jobs.queue import enqueue

router = APIRouter(prefix="/api/articles", tags=["articles"])

SessionDep = Annotated[Session, Depends(get_session)]
UserIdDep = Annotated[uuid.UUID, Depends(get_current_user_id)]

# 極端に長い URL による無用なリソース消費を避けるための上限。
MAX_URL_LENGTH = 2048
_ALLOWED_URL_SCHEMES = ("http://", "https://")

# 関心記事一覧（`GET /api/articles`）に出す origin（`PROJECT_SPEC.md` §6.3）。
# read_full / clicked は暗黙の関心シグナルであり、ユーザーが明示的に関心記事へ
# 追加した経路ではないため一覧には出さない。
INTEREST_LIST_ORIGIN_VALUES = frozenset(
    origin.value for origin in (ArticleOrigin.MANUAL, ArticleOrigin.GOOD, ArticleOrigin.SAVED)
)

# 一覧のページングの既定値・上限値。`recommendations.py` の GET /api/feed とは
# 別物（推薦の構成比等とは無関係な単純な一覧のため）で、スコアリング設定
# （config/scoring.yaml）とは独立に持つ。
DEFAULT_INTEREST_LIST_PAGE_SIZE = 20
MAX_INTEREST_LIST_PAGE_SIZE = 100

# cursor（デコード後）中の created_at と id の区切り文字。
_INTEREST_CURSOR_SEPARATOR = ":"

# cursor がデコード後に含む raw 文字列の最大長。ISO8601 の datetime
# （タイムゾーン付きで最大 32 文字程度）+ 区切り 1 文字 + UUID（36 文字）に
# 余裕を持たせつつ、極端に長い文字列を `datetime.fromisoformat` /
# `uuid.UUID` へ渡さないための安全弁（`recommendations.py` の cursor 検証と同じ方針）。
_MAX_INTEREST_CURSOR_RAW_LENGTH = 96
INTEREST_CURSOR_MAX_LENGTH = math.ceil(_MAX_INTEREST_CURSOR_RAW_LENGTH / 3) * 4


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


class InterestArticleItem(BaseModel):
    """関心記事一覧（`GET /api/articles`）1 件のレスポンス（`PROJECT_SPEC.md` §6.3）。"""

    article_id: uuid.UUID
    origin: ArticleOrigin
    registered_at: datetime
    title: str
    translated_title: str | None
    canonical_url: str
    original_url: str
    source_domain: str
    language: str | None
    topics: list[str]
    domain: str | None
    category: str | None
    content_type: str | None
    is_primary_source: bool
    published_at: datetime | None


class InterestArticleListResponse(BaseModel):
    """関心記事一覧のレスポンス。"""

    items: list[InterestArticleItem]
    # 次ページが無ければ null。
    next_cursor: str | None


class InvalidInterestCursorError(Exception):
    """壊れた関心記事一覧の cursor 文字列を検出したときの例外。"""


def _encode_interest_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    """`user_articles` の `created_at` と `id` から、不透明な cursor 文字列を作る。"""
    raw = f"{created_at.isoformat()}{_INTEREST_CURSOR_SEPARATOR}{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_interest_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """cursor 文字列を `created_at` と `id` へ復元する。

    壊れた cursor（長すぎる・base64 として不正・区切り文字が無い・
    datetime/UUID として不正）はすべて `InvalidInterestCursorError` にまとめる。
    """
    if len(cursor) > INTEREST_CURSOR_MAX_LENGTH:
        raise InvalidInterestCursorError

    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        created_at_text, row_id_text = raw.rsplit(_INTEREST_CURSOR_SEPARATOR, 1)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidInterestCursorError from exc

    try:
        created_at = datetime.fromisoformat(created_at_text)
        row_id = uuid.UUID(row_id_text)
    except ValueError as exc:
        raise InvalidInterestCursorError from exc
    return created_at, row_id


def _build_interest_article_item(
    user_article: UserArticle, article: Article
) -> InterestArticleItem:
    """DB から読み出した `UserArticle` / `Article` を API レスポンス項目にする。"""
    return InterestArticleItem(
        article_id=article.id,
        origin=ArticleOrigin(user_article.origin),
        registered_at=user_article.created_at,
        title=article.title,
        translated_title=article.translated_title,
        canonical_url=article.canonical_url,
        original_url=article.original_url,
        source_domain=article.source_domain,
        language=article.language,
        topics=list(article.topics),
        domain=article.domain,
        category=article.category,
        content_type=article.content_type,
        is_primary_source=article.is_primary_source,
        published_at=article.published_at,
    )


def _build_interest_article_query(
    user_id: uuid.UUID,
    *,
    origin: Sequence[ArticleOrigin] | None,
    domain: str | None,
    category: str | None,
    source_domain: str | None,
    language: str | None,
    registered_from: datetime | None,
    registered_to: datetime | None,
    is_primary_source: bool | None,
) -> Select[tuple[UserArticle, Article]]:
    """フィルター条件を反映した、関心記事一覧の SELECT 文を組み立てる。

    `origin` フィルターは常に適用する `INTEREST_LIST_ORIGIN_VALUES`（一覧に
    出す3経路）との積集合にする。read_full/clicked だけを指定した場合は
    合致件数 0 件になる（一覧にそもそも出ない origin のため）。
    """
    allowed_origins = INTEREST_LIST_ORIGIN_VALUES
    if origin:
        allowed_origins = {value.value for value in origin} & INTEREST_LIST_ORIGIN_VALUES

    filters = [
        UserArticle.user_id == user_id,
        UserArticle.origin.in_(allowed_origins),
    ]
    if domain is not None:
        filters.append(Article.domain == domain)
    if category is not None:
        filters.append(Article.category == category)
    if source_domain is not None:
        filters.append(Article.source_domain == source_domain)
    if language is not None:
        filters.append(Article.language == language)
    if registered_from is not None:
        filters.append(UserArticle.created_at >= registered_from)
    if registered_to is not None:
        filters.append(UserArticle.created_at <= registered_to)
    if is_primary_source is not None:
        filters.append(Article.is_primary_source == is_primary_source)

    return (
        select(UserArticle, Article)
        .join(Article, Article.id == UserArticle.article_id)
        .where(*filters)
    )


@router.get("", response_model=InterestArticleListResponse)
def list_interest_articles(
    session: SessionDep,
    user_id: UserIdDep,
    origin: Annotated[list[ArticleOrigin] | None, Query(description="登録方法。複数指定可")] = None,
    domain: Annotated[str | None, Query(description="ジャンル大分類")] = None,
    category: Annotated[str | None, Query(description="ジャンル中分類")] = None,
    source_domain: Annotated[str | None, Query(description="情報源")] = None,
    language: Annotated[str | None, Query(description="言語")] = None,
    registered_from: Annotated[
        datetime | None, Query(description="登録日時の期間（下限、含む）")
    ] = None,
    registered_to: Annotated[
        datetime | None, Query(description="登録日時の期間（上限、含む）")
    ] = None,
    is_primary_source: Annotated[bool | None, Query(description="公式 / 非公式")] = None,
    cursor: Annotated[
        str | None, Query(description="前回レスポンスの next_cursor をそのまま渡す")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_INTEREST_LIST_PAGE_SIZE)] = (
        DEFAULT_INTEREST_LIST_PAGE_SIZE
    ),
) -> InterestArticleListResponse:
    """関心記事一覧を返す（`PROJECT_SPEC.md` §6.3）。

    手動登録・Good・保存の3経路のみを対象にし、登録日時（`user_articles.created_at`）
    降順で返す。タイブレークに `user_articles.id` を使い、cursor はこの2つの組から作る。
    """
    if (
        registered_from is not None
        and registered_to is not None
        and registered_from > registered_to
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="registered_from は registered_to 以前である必要があります",
        )

    after: tuple[datetime, uuid.UUID] | None = None
    if cursor is not None:
        try:
            after = _decode_interest_cursor(cursor)
        except InvalidInterestCursorError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="cursor が不正です"
            ) from exc

    statement = _build_interest_article_query(
        user_id,
        origin=origin,
        domain=domain,
        category=category,
        source_domain=source_domain,
        language=language,
        registered_from=registered_from,
        registered_to=registered_to,
        is_primary_source=is_primary_source,
    )
    if after is not None:
        statement = statement.where(tuple_(UserArticle.created_at, UserArticle.id) < after)

    # 次ページの有無を判定するため、要求件数より 1 件多く取得する
    # （`recommendations.py` の `get_feed` と同じ方針）。
    rows = session.execute(
        statement.order_by(UserArticle.created_at.desc(), UserArticle.id.desc()).limit(limit + 1)
    ).all()
    has_next_page = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor = (
        _encode_interest_cursor(page_rows[-1][0].created_at, page_rows[-1][0].id)
        if has_next_page
        else None
    )
    return InterestArticleListResponse(
        items=[
            _build_interest_article_item(user_article, article)
            for user_article, article in page_rows
        ],
        next_cursor=next_cursor,
    )


@router.delete("/{article_id}/interest", status_code=status.HTTP_204_NO_CONTENT)
def delete_interest_article(
    article_id: uuid.UUID,
    session: SessionDep,
    user_id: UserIdDep,
) -> Response:
    """記事を関心記事一覧から除外する。

    `user_articles` の該当行を削除するだけで、`article_feedback` には触れない
    （Bad フィードバックも付けない）。「興味がない」（Bad）と「間違って登録した」
    （一覧からの除外）は別の意思表示のため区別する（Issue #14 ヒアリング済み決定事項）。
    """
    user_article = session.scalar(
        select(UserArticle).where(
            UserArticle.user_id == user_id, UserArticle.article_id == article_id
        )
    )
    if user_article is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="関心記事が見つかりません"
        )

    session.delete(user_article)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
