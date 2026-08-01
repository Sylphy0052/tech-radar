"""記事取得時に情報源が分類されることを検証する（`PROJECT_SPEC.md` §10, §11）。"""

from __future__ import annotations

import socket

import httpx
import pytest
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import SourceType
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.service import ingest_article
from techradar.sources.config import RegistryConfig
from techradar.sources.service import seed_source_registry
from tests.test_fetcher_extract import article_html
from tests.test_fetcher_http import mock_client
from tests.test_fetcher_ssrf import fake_getaddrinfo

CONFIG = RegistryConfig.model_validate(
    {
        "authority_by_source_type": {
            "official_documentation": 1.0,
            "personal_article": 0.6,
            "unknown": 0.35,
        },
        "fallback": {
            "default_source_type": "unknown",
            "domains": {"blog.example.net": "personal_article"},
        },
        "entities": [
            {
                "name": "Example",
                "rules": [{"domain": "docs.example.com", "type": "official_documentation"}],
            }
        ],
    }
)


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """canonical を持たない記事 HTML を返すスタブ。

    canonical が無いので、要求 URL の正規形がそのまま `canonical_url` になる。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=article_html(),
        )

    monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))


class TestSourceClassificationOnIngest:
    def test_marks_a_registered_domain_as_a_primary_source(
        self, db_session: Session, public_dns, transport, settings: Settings
    ):
        # Arrange
        seed_source_registry(db_session, CONFIG)

        # Act
        result = ingest_article(
            db_session,
            "https://docs.example.com/guide/1",
            settings=settings,
            registry_config=CONFIG,
        )

        # Assert
        assert result.article.source_type == SourceType.OFFICIAL_DOCUMENTATION.value
        assert result.article.source_authority == 1.0
        assert result.article.is_primary_source is True

    def test_uses_the_fallback_for_an_unregistered_domain(
        self, db_session: Session, public_dns, transport, settings: Settings
    ):
        # Arrange
        seed_source_registry(db_session, CONFIG)

        # Act
        result = ingest_article(
            db_session,
            "https://blog.example.net/posts/1",
            settings=settings,
            registry_config=CONFIG,
        )

        # Assert
        assert result.article.source_type == SourceType.PERSONAL_ARTICLE.value
        assert result.article.source_authority == 0.6
        assert result.article.is_primary_source is False

    def test_leaves_no_article_unclassified(
        self, db_session: Session, public_dns, transport, settings: Settings
    ):
        # Arrange — レジストリが空でも既定値のままにしない
        # (source_authority = 0 だと推薦時に不当に沈む)

        # Act
        result = ingest_article(
            db_session,
            "https://unknown.example.org/posts/1",
            settings=settings,
            registry_config=CONFIG,
        )

        # Assert
        assert result.article.source_type == SourceType.UNKNOWN.value
        assert result.article.source_authority == 0.35
