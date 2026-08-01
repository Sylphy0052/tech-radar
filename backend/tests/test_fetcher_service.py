"""記事の取得・保存・重複回避を検証する結合テスト。"""

from __future__ import annotations

import socket

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db import Article
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.service import find_existing_article, ingest_article
from tests.test_fetcher_extract import article_html
from tests.test_fetcher_http import mock_client
from tests.test_fetcher_ssrf import fake_getaddrinfo


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def counting_transport(monkeypatch: pytest.MonkeyPatch):
    """取得回数を数えられる HTTP スタブを返す。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=article_html(),
        )

    monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))
    return calls


class TestIngestArticle:
    def test_stores_extracted_fields(
        self, db_session: Session, public_dns, counting_transport, settings: Settings
    ):
        # Arrange / Act
        result = ingest_article(db_session, "https://example.com/posts/1", settings=settings)

        # Assert
        assert result.was_fetched is True
        article = result.article
        assert article.title == "MCP サーバー実装ガイド"
        assert article.body is not None
        assert "Model Context Protocol" in article.body
        assert article.language == "ja"
        assert article.published_at is not None
        assert article.source_domain == "example.com"
        assert article.body_hash

    def test_does_not_refetch_the_same_url(
        self, db_session: Session, public_dns, counting_transport, settings: Settings
    ):
        # Arrange
        first = ingest_article(db_session, "https://example.com/posts/1", settings=settings)

        # Act — 同じ URL を再登録する
        second = ingest_article(db_session, "https://example.com/posts/1", settings=settings)

        # Assert — HTTP 取得は 1 回だけ
        assert len(counting_transport) == 1
        assert second.was_fetched is False
        assert second.article.id == first.article.id

    def test_treats_tracking_variants_as_the_same_article(
        self, db_session: Session, public_dns, counting_transport, settings: Settings
    ):
        # Arrange
        ingest_article(db_session, "https://example.com/posts/1", settings=settings)

        # Act — 計測パラメータとフラグメント違いの同じ記事
        second = ingest_article(
            db_session,
            "https://example.com/posts/1/?utm_source=twitter#intro",
            settings=settings,
        )

        # Assert
        assert len(counting_transport) == 1
        assert second.was_fetched is False
        stored = db_session.scalars(select(Article)).all()
        assert len(stored) == 1

    def test_keeps_original_url_alongside_canonical(
        self, db_session: Session, public_dns, counting_transport, settings: Settings
    ):
        # Arrange / Act — 登録時の URL も残す（利用者が入力した形を失わない）
        result = ingest_article(
            db_session, "https://example.com/posts/1?utm_source=x", settings=settings
        )

        # Assert
        assert result.article.original_url == "https://example.com/posts/1?utm_source=x"
        assert result.article.canonical_url == "https://example.com/posts/1"

    def test_stores_distinct_articles_separately(
        self, db_session: Session, public_dns, counting_transport, settings: Settings
    ):
        # Arrange / Act
        ingest_article(db_session, "https://example.com/posts/1", settings=settings)
        ingest_article(db_session, "https://example.com/posts/2", settings=settings)

        # Assert
        assert len(counting_transport) == 2
        assert len(db_session.scalars(select(Article)).all()) == 2


class TestFindExistingArticle:
    def test_matches_by_normalized_url(
        self, db_session: Session, public_dns, counting_transport, settings: Settings
    ):
        # Arrange
        ingest_article(db_session, "https://example.com/posts/1", settings=settings)

        # Act
        found = find_existing_article(db_session, "HTTPS://Example.com:443/posts/1/?utm_medium=rss")

        # Assert
        assert found is not None

    def test_returns_none_for_unknown_url(self, db_session: Session):
        # Arrange / Act / Assert
        assert find_existing_article(db_session, "https://example.com/unknown") is None
