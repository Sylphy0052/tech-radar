"""記事の取得・抽出・保存をまとめる。

同一 URL の再登録では再フェッチしない（`PROJECT_SPEC.md` §24 コスト管理）。
判定は正規化 URL で行い、表記ゆれのある URL も同じ記事として扱う。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db import Article
from techradar.db.enums import JobStatus
from techradar.fetcher.extract import ExtractedArticle, extract_article
from techradar.fetcher.http import fetch_page
from techradar.fetcher.url import normalize_url
from techradar.sources.config import RegistryConfig, get_registry_config
from techradar.sources.service import classify_with_registry


@dataclass(frozen=True)
class IngestResult:
    """登録結果。

    `was_fetched` が False なら既存レコードを再利用したことを表す。
    """

    article: Article
    was_fetched: bool


def find_existing_article(session: Session, url: str) -> Article | None:
    """正規化 URL で既存記事を探す。"""
    normalized = normalize_url(url)
    return session.scalars(select(Article).where(Article.canonical_url == normalized)).one_or_none()


def _apply_extraction(article: Article, extracted: ExtractedArticle, original_url: str) -> None:
    """抽出結果を記事へ反映する。"""
    article.canonical_url = extracted.canonical_url
    article.original_url = original_url
    article.title = extracted.title
    article.body = extracted.body
    article.body_hash = extracted.body_hash
    article.published_at = extracted.published_at
    article.language = extracted.language
    article.author = extracted.author
    article.source_domain = urlsplit(extracted.canonical_url).hostname or ""


def _apply_source_classification(
    session: Session,
    article: Article,
    registry_config: RegistryConfig,
) -> None:
    """情報源の種別と authority を記事へ反映する（`PROJECT_SPEC.md` §10, §11）。

    ここで埋めないと `source_authority` が既定値のままになり、推薦時に
    一次情報を優先できない。
    """
    classification = classify_with_registry(session, article.canonical_url, registry_config)
    article.source_type = classification.source_type.value
    article.source_authority = classification.authority_score
    article.is_primary_source = classification.is_primary_source


def ingest_article(
    session: Session,
    url: str,
    *,
    settings: Settings | None = None,
    registry_config: RegistryConfig | None = None,
) -> IngestResult:
    """URL から記事を取得して保存する。

    既に同じ正規化 URL の記事があれば取得せず既存を返す。
    ネットワークアクセスと LLM 呼び出しを繰り返さないため。

    Args:
        registry_config: 情報源レジストリの設定。省略時は同梱設定を使う。
    """
    existing = find_existing_article(session, url)
    if existing is not None:
        return IngestResult(article=existing, was_fetched=False)

    page = fetch_page(url, settings=settings)
    extracted = extract_article(page.html, page.final_url)

    # canonical がリダイレクト後の URL を指す場合、その正規形で再度重複を確認する。
    already_stored = session.scalars(
        select(Article).where(Article.canonical_url == extracted.canonical_url)
    ).one_or_none()
    if already_stored is not None:
        return IngestResult(article=already_stored, was_fetched=True)

    article = Article(
        canonical_url=extracted.canonical_url,
        original_url=url,
        title=extracted.title,
        source_domain="",
        # 解析待ちであることを明示する。NULL だと未解析か不明かを区別できない。
        analysis_status=JobStatus.PENDING,
    )
    _apply_extraction(article, extracted, url)
    _apply_source_classification(session, article, registry_config or get_registry_config())
    session.add(article)
    session.flush()
    return IngestResult(article=article, was_fetched=True)
