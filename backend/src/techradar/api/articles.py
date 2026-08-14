"""URL 登録 API（`PROJECT_SPEC.md` §6.2, §20, Issue #12）。

MVP は単一ユーザー・認証なし（§22）のため、登録者は常に
`api.deps.get_current_user_id` が返す ID を使う。認証を導入する際は
その依存の実装を差し替えればよい。
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from techradar.api.bulk_import import (
    MAX_BULK_IMPORT_FILE_BYTES,
    MAX_BULK_IMPORT_URL_COUNT,
    ParsedUrlLine,
    has_allowed_bulk_import_extension,
    parse_url_lines,
    truncate_line_preview,
    validate_bulk_import_url,
)
from techradar.api.deps import get_current_user_id, get_session
from techradar.api.query_filters import MAX_PAGE_NUMBER, reject_oversized_list
from techradar.db.enums import ArticleOrigin, JobStatus, JobType
from techradar.db.errors import is_unique_violation
from techradar.db.models import Article, ArticleRegistration, UserArticle
from techradar.db.query import LIKE_ESCAPE_CHAR, escape_like_pattern
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

# 関心記事一覧のテキストフィルター（q/domain/category/source_domain/language）の上限。
# 実データはこれよりずっと短いが、際限なく長い文字列を DB 比較へ渡さないための安全弁。
# `topics` / `technologies` の要素ごとの長さにも同じ値を使う。
INTEREST_LIST_TEXT_FILTER_MAX_LENGTH = 256

# topics / technologies の件数上限（Issue #91）。`GET /api/feed` の
# `FEED_LIST_FILTER_MAX_ITEMS` と同じ値・同じ検証
# （`query_filters.reject_oversized_list`）にする。
INTEREST_LIST_FILTER_MAX_ITEMS = 20


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


def _find_existing_normalized_urls(
    session: Session, user_id: uuid.UUID, normalized_urls: Sequence[str]
) -> set[str]:
    """同一ユーザーで既に登録済みの正規化 URL を1クエリでまとめて引く。

    一括登録は最大 `MAX_BULK_IMPORT_URL_COUNT` 行を扱うため、行ごとに
    `_find_existing_registration` を呼ぶと件数ぶんの DB 往復がリクエスト中に
    直列で走る。重複判定に必要なのは正規化 URL の集合だけなので、行そのものは
    取得しない。

    IN 句の要素数は呼び出し側が抑える責務を持つ。一括登録は
    `MAX_BULK_IMPORT_URL_COUNT` を超えるファイルを DB へ触れる前に 413 で
    弾いているため、ここへ渡る件数はその上限以下になる。上限を大きく緩める
    場合は分割して引くことを検討する。重複した URL が混ざっていても安全な
    よう集合へ落としてから渡す。
    """
    if not normalized_urls:
        # 空の IN 句を組み立てて無駄な往復を作らない。
        return set()

    statement = select(ArticleRegistration.normalized_url).where(
        ArticleRegistration.user_id == user_id,
        ArticleRegistration.normalized_url.in_(set(normalized_urls)),
    )
    return set(session.scalars(statement))


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


# 一括登録アップロードの読み込みに使うチャンクサイズ。`MAX_BULK_IMPORT_FILE_BYTES`
# より十分小さくし、上限超過を早期に検出できるようにする。
_BULK_IMPORT_READ_CHUNK_BYTES = 65536


class BulkImportErrorItem(BaseModel):
    """一括登録でエラーとして扱った行の情報。"""

    line_number: int
    line: str
    reason: str


class BulkArticleImportResponse(BaseModel):
    """URL リストファイルの一括登録結果（`POST /api/articles/bulk`）。"""

    created: list[ArticleRegistrationResponse]
    created_count: int
    duplicate_count: int
    error_count: int
    errors: list[BulkImportErrorItem]


def _read_upload_within_limit(upload: UploadFile) -> bytes:
    """アップロードされたファイルを、上限（`MAX_BULK_IMPORT_FILE_BYTES`）を
    超えたら打ち切りながら読む。

    全内容を読み切ってからサイズを判定すると、上限を大きく超えるファイルでも
    一時的に全体をメモリへ載せてしまう。読みながら判定することでそれを避ける。
    """
    chunks: list[bytes] = []
    total_size = 0
    while True:
        chunk = upload.file.read(_BULK_IMPORT_READ_CHUNK_BYTES)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > MAX_BULK_IMPORT_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"アップロードファイルが上限（{MAX_BULK_IMPORT_FILE_BYTES}バイト）を超えています",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_bulk_import_text(content: bytes) -> str:
    """アップロード内容を UTF-8 としてデコードする。デコードできなければ 422。"""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ファイルをUTF-8として読み取れませんでした",
        ) from exc


def _register_bulk_import_line(
    session: Session, user_id: uuid.UUID, url: str, normalized_url: str
) -> ArticleRegistration | None:
    """一括登録の1行ぶんを挿入する。同時挿入と競合したら None を返し重複として扱う。

    重複していないことは呼び出し側が `_find_existing_normalized_urls` の結果で
    判定済みだが、その突き合わせと挿入の間に別リクエストが同じ URL を挿入する
    可能性（TOCTOU）は残る。このとき `_create_registration` の単純な
    `session.rollback()` を一括処理でそのまま使うと、そこまでに flush 済みの
    他の行の登録ごと巻き戻ってしまう。ここでは `session.begin_nested()`
    （SAVEPOINT）で当該行だけを隔離し、`with` ブロックの外側で例外を捕まえる
    ことで、SAVEPOINT へのロールバックだけが起きた状態でハンドリングする。
    """
    # ジョブの payload に載せるため、flush を待たずに ID を決めておく。
    registration_id = uuid.uuid4()
    registration = ArticleRegistration(
        id=registration_id,
        user_id=user_id,
        url=url,
        normalized_url=normalized_url,
        status=JobStatus.PENDING.value,
    )
    try:
        with session.begin_nested():
            # ジョブを先に積み、`job_id` を埋めた状態で登録行を挿入する。登録行を
            # 挿入してから `job_id` を代入すると、行ごとに UPDATE が走るうえ
            # `updated_at` がサーバー側で再計算されて期限切れになり、応答を組み立てる
            # ときに行ごとの再読込まで発生する。
            job = enqueue(
                session,
                JobType.FETCH_ARTICLE,
                {"registration_id": str(registration_id), "url": url},
            )
            registration.job_id = job.id
            session.add(registration)
            session.flush()
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        return None

    return registration


@dataclass(frozen=True)
class _ValidatedBulkImportLine:
    """検証と正規化を通り、DB との突き合わせを待っている1行。"""

    parsed_line: ParsedUrlLine
    normalized_url: str


@dataclass
class _BulkImportScreening:
    """DB へ触れる前に確定できる振り分けの結果。"""

    lines: list[_ValidatedBulkImportLine]
    errors: list[BulkImportErrorItem]
    duplicate_count: int


def _screen_bulk_import_lines(parsed_lines: Sequence[ParsedUrlLine]) -> _BulkImportScreening:
    """抽出済みの行を検証・正規化し、ファイル内で重複する行を落とす。

    DB を引かずに確定できる振り分け（不正な行・ファイル内の重複）をここで
    済ませることで、残った行の既存登録を1クエリでまとめて突き合わせられる。
    """
    lines: list[_ValidatedBulkImportLine] = []
    errors: list[BulkImportErrorItem] = []
    duplicate_count = 0
    seen_normalized_urls: set[str] = set()

    for parsed_line in parsed_lines:
        invalid_reason = validate_bulk_import_url(
            parsed_line.url, allowed_schemes=_ALLOWED_URL_SCHEMES, max_length=MAX_URL_LENGTH
        )
        if invalid_reason is not None:
            errors.append(
                BulkImportErrorItem(
                    line_number=parsed_line.line_number,
                    line=truncate_line_preview(parsed_line.original_line),
                    reason=invalid_reason,
                )
            )
            continue

        normalized_url = normalize_url(parsed_line.url)
        if normalized_url in seen_normalized_urls:
            duplicate_count += 1
            continue
        seen_normalized_urls.add(normalized_url)
        lines.append(
            _ValidatedBulkImportLine(parsed_line=parsed_line, normalized_url=normalized_url)
        )

    return _BulkImportScreening(lines=lines, errors=errors, duplicate_count=duplicate_count)


def _process_bulk_import_lines(
    session: Session, user_id: uuid.UUID, parsed_lines: Sequence[ParsedUrlLine]
) -> BulkArticleImportResponse:
    """抽出済みの行を出現順に処理し、登録・重複・エラーへ振り分ける。"""
    screening = _screen_bulk_import_lines(parsed_lines)
    existing_normalized_urls = _find_existing_normalized_urls(
        session, user_id, [line.normalized_url for line in screening.lines]
    )

    created: list[ArticleRegistration] = []
    duplicate_count = screening.duplicate_count

    for line in screening.lines:
        if line.normalized_url in existing_normalized_urls:
            duplicate_count += 1
            continue

        registration = _register_bulk_import_line(
            session, user_id, line.parsed_line.url, line.normalized_url
        )
        if registration is None:
            duplicate_count += 1
            continue
        created.append(registration)

    return BulkArticleImportResponse(
        created=[ArticleRegistrationResponse.model_validate(r) for r in created],
        created_count=len(created),
        duplicate_count=duplicate_count,
        error_count=len(screening.errors),
        errors=screening.errors,
    )


@router.post("/bulk", response_model=BulkArticleImportResponse)
def bulk_import_article_registrations(
    session: SessionDep,
    user_id: UserIdDep,
    file: Annotated[UploadFile, File(description="URLリストファイル（.md / .txt、UTF-8）")],
) -> BulkArticleImportResponse:
    """URL リストファイルをアップロードし、行ごとに URL を抽出して一括登録する（Issue #39）。

    1件でも不正な行があっても全体を拒否せず、抽出できた URL をファイル内の出現順に
    処理する。ファイルサイズ・抽出後の URL 件数が上限を超える場合は DB を一切
    変更せず 413 を返す（`_read_upload_within_limit` / 抽出後件数チェックのどちらも、
    行ごとの DB 処理を始める前に完了させることで担保する）。
    """
    if not has_allowed_bulk_import_extension(file.filename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="対応していないファイル形式です（.md / .txt のみ受け付けます）",
        )

    content = _read_upload_within_limit(file)
    text = _decode_bulk_import_text(content)
    parsed_lines = parse_url_lines(text)
    if len(parsed_lines) > MAX_BULK_IMPORT_URL_COUNT:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"URLの件数が上限（{MAX_BULK_IMPORT_URL_COUNT}件）を超えています",
        )

    result = _process_bulk_import_lines(session, user_id, parsed_lines)
    # 応答を返す前にコミットする（`create_article_registration` と同じ理由）。
    session.commit()
    return result


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
    """関心記事一覧のレスポンス（ページングは Issue #91 で番号付きへ変更）。

    `GET /api/feed` の `FeedResponse` と同じ形にそろえ、画面側の
    `components/ui/Pagination` を両方から同じように使えるようにする。
    """

    items: list[InterestArticleItem]
    # 絞り込み後の総件数（現在のページの件数ではない）。
    total_count: int
    # 要求されたページ番号（1 始まり）。範囲外でもそのまま返す。
    page: int
    # 1 ページあたりの件数（`limit` クエリパラメータの値）。
    page_size: int
    # `total_count` を `page_size` で割って切り上げた総ページ数。0 件なら 0。
    total_pages: int


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


def _reject_naive_datetime(value: datetime | None, *, param_name: str) -> None:
    """タイムゾーン情報の無い（naive な）datetime を 422 で拒否する。

    `user_articles.created_at` は `DateTime(timezone=True)` 列のため、naive な
    datetime を比較に使うとセッションのタイムゾーン設定に暗黙依存してしまう。

    `AfterValidator` をクエリパラメータの `Annotated` に付ける方式も検討したが、
    このモジュールは `from __future__ import annotations` を使っており、
    pydantic がその組み合わせでアノテーションを `ForwardRef` のまま解決できず
    `PydanticUserError` になった（実機で確認済み）ため、既存の
    `registered_from > registered_to` 検証と同じ「関数本体で明示チェックして
    HTTPException を送出する」方式に統一する。
    """
    if value is not None and value.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{param_name} はタイムゾーン付きの日時で指定してください",
        )


def _resolve_allowed_origins(origin: Sequence[ArticleOrigin] | None) -> frozenset[str]:
    """`origin` クエリを、一覧に出す3経路（`INTEREST_LIST_ORIGIN_VALUES`）との積集合にする。

    read_full/clicked だけを指定した場合は空集合になる（一覧にそもそも出ない
    origin のため）。呼び出し側はこれが空なら DB へ問い合わせず 200 + 空配列を
    返すショートサーキットを行う（`UserArticle.origin.in_(set())` という
    常に偽になる IN 句を SQLAlchemy に発行させないため）。
    """
    if not origin:
        return INTEREST_LIST_ORIGIN_VALUES
    return frozenset(value.value for value in origin) & INTEREST_LIST_ORIGIN_VALUES


def _build_interest_article_filters(
    user_id: uuid.UUID,
    *,
    allowed_origins: frozenset[str],
    query: str | None,
    topics: list[str] | None,
    technologies: list[str] | None,
    domain: str | None,
    category: str | None,
    source_domain: str | None,
    language: str | None,
    registered_from: datetime | None,
    registered_to: datetime | None,
    is_primary_source: bool | None,
) -> list[ColumnElement[bool]]:
    """フィルター条件を WHERE 句のリストにする。

    件数を数えるクエリと 1 ページ分を読むクエリの両方が同じ条件を使うため、
    条件だけを組み立てて返す。`allowed_origins` は空でない前提
    （空集合なら呼び出し側がショートサーキットする）。

    検索語とタグの絞り込みは `recommendation/service.py` の `load_candidates`
    （`GET /api/feed`）と同じ形にそろえる。検索語は title / translated_title /
    summary_ja のいずれかへの部分一致（大文字小文字を区別しない）、
    topics / technologies は JSONB の包含（`@>`）で「指定した全てを含む」
    （AND）を表す。

    検索語に含まれる LIKE の特殊文字（`%` / `_`）は `db/query.escape_like_pattern`
    でエスケープしてから埋め込む。`api/sources.py` と `load_candidates` にも
    同じ関数を使っている（Issue #94）。
    """
    filters: list[ColumnElement[bool]] = [
        UserArticle.user_id == user_id,
        UserArticle.origin.in_(allowed_origins),
    ]
    if query:
        pattern = f"%{escape_like_pattern(query)}%"
        filters.append(
            or_(
                Article.title.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                Article.translated_title.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
                Article.summary_ja.ilike(pattern, escape=LIKE_ESCAPE_CHAR),
            )
        )
    if topics:
        filters.append(Article.topics.contains(topics))
    if technologies:
        filters.append(Article.technologies.contains(technologies))
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

    return filters


@router.get("", response_model=InterestArticleListResponse)
def list_interest_articles(
    session: SessionDep,
    user_id: UserIdDep,
    origin: Annotated[list[ArticleOrigin] | None, Query(description="登録方法。複数指定可")] = None,
    q: Annotated[
        str | None,
        Query(
            description=(
                "検索語。title/translated_title/summary_jaへの部分一致（大文字小文字を区別しない）"
            ),
            max_length=INTEREST_LIST_TEXT_FILTER_MAX_LENGTH,
        ),
    ] = None,
    topics: Annotated[
        list[str] | None,
        Query(description="トピック。複数指定時は指定した全てを含む記事に絞る（AND）"),
    ] = None,
    technologies: Annotated[
        list[str] | None,
        Query(description="技術タグ。複数指定時は指定した全てを含む記事に絞る（AND）"),
    ] = None,
    domain: Annotated[
        str | None,
        Query(description="ジャンル大分類", max_length=INTEREST_LIST_TEXT_FILTER_MAX_LENGTH),
    ] = None,
    category: Annotated[
        str | None,
        Query(description="ジャンル中分類", max_length=INTEREST_LIST_TEXT_FILTER_MAX_LENGTH),
    ] = None,
    source_domain: Annotated[
        str | None,
        Query(description="情報源", max_length=INTEREST_LIST_TEXT_FILTER_MAX_LENGTH),
    ] = None,
    language: Annotated[
        str | None,
        Query(description="言語", max_length=INTEREST_LIST_TEXT_FILTER_MAX_LENGTH),
    ] = None,
    registered_from: Annotated[
        datetime | None, Query(description="登録日時の期間（下限、含む）")
    ] = None,
    registered_to: Annotated[
        datetime | None, Query(description="登録日時の期間（上限、含む）")
    ] = None,
    is_primary_source: Annotated[bool | None, Query(description="公式 / 非公式")] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE_NUMBER, description="1始まりのページ番号")] = 1,
    limit: Annotated[int, Query(ge=1, le=MAX_INTEREST_LIST_PAGE_SIZE)] = (
        DEFAULT_INTEREST_LIST_PAGE_SIZE
    ),
) -> InterestArticleListResponse:
    """関心記事一覧を返す（`PROJECT_SPEC.md` §6.3、検索・絞り込み・ページングは Issue #91）。

    手動登録・Good・保存の3経路のみを対象にし、登録日時（`user_articles.created_at`）
    降順で返す。タイブレークに `user_articles.id` を使う。

    ページングは番号付き（`page` / `limit`）で、`GET /api/feed` と同じく offset として
    扱う。範囲外の `page` はエラーにせず空の `items` を返す。Issue #91 以前は cursor
    方式だったが、目的のページへ直接移動できるようにするため置き換えた。
    """
    # naive な datetime 同士でも `>` 自体は例外を出さないが、片方だけ tz-aware だと
    # 比較で TypeError になる。ここで先に弾いておくことで、直後の順序比較を安全にする。
    _reject_naive_datetime(registered_from, param_name="registered_from")
    _reject_naive_datetime(registered_to, param_name="registered_to")

    if (
        registered_from is not None
        and registered_to is not None
        and registered_from > registered_to
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="registered_from は registered_to 以前である必要があります",
        )

    for param_name, values in (("topics", topics), ("technologies", technologies)):
        reject_oversized_list(
            values,
            param_name=param_name,
            max_items=INTEREST_LIST_FILTER_MAX_ITEMS,
            max_item_length=INTEREST_LIST_TEXT_FILTER_MAX_LENGTH,
        )

    allowed_origins = _resolve_allowed_origins(origin)
    if not allowed_origins:
        # 指定された origin が一覧対象の3経路と1つも重ならない
        # （例: read_full のみ指定）。DB へ問い合わせるまでもなく空配列が確定するため、
        # 常に偽になる `IN ()` 句を発行せずショートサーキットする。
        return InterestArticleListResponse(
            items=[], total_count=0, page=page, page_size=limit, total_pages=0
        )

    filters = _build_interest_article_filters(
        user_id,
        allowed_origins=allowed_origins,
        query=q,
        topics=topics,
        technologies=technologies,
        domain=domain,
        category=category,
        source_domain=source_domain,
        language=language,
        registered_from=registered_from,
        registered_to=registered_to,
        is_primary_source=is_primary_source,
    )

    total_count = (
        session.scalar(
            select(func.count())
            .select_from(UserArticle)
            .join(Article, Article.id == UserArticle.article_id)
            .where(*filters)
        )
        or 0
    )
    total_pages = math.ceil(total_count / limit) if total_count > 0 else 0

    rows = session.execute(
        select(UserArticle, Article)
        .join(Article, Article.id == UserArticle.article_id)
        .where(*filters)
        .order_by(UserArticle.created_at.desc(), UserArticle.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return InterestArticleListResponse(
        items=[
            _build_interest_article_item(user_article, article) for user_article, article in rows
        ],
        total_count=total_count,
        page=page,
        page_size=limit,
        total_pages=total_pages,
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
    # 応答を返す前に明示的にコミットする。`get_session`（`session_scope`）は
    # 正常終了時に自動コミットするが、その後処理が走るのはレスポンス送信より後になる
    # （`create_article_registration` と同じ理由）。ここで委ねると、204 を受け取った
    # UI が直後に一覧を再取得したとき、削除がまだコミットされておらず該当行が
    # 残って見えるおそれがある。
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
