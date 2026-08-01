"""HTTP 取得の制限とリダイレクト検証を確かめる。

ネットワークへは出ず、`httpx.MockTransport` で応答を組み立てる。
"""

from __future__ import annotations

import socket

import httpx
import pytest

from techradar.config import Settings
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.errors import (
    FetchError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
)
from techradar.fetcher.http import fetch_page
from tests.test_fetcher_ssrf import fake_getaddrinfo

HTML = "<html lang='en'><head><title>T</title></head><body><p>hello</p></body></html>"


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    """すべてのホストが公開 IP に解決される状態にする。"""
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, fetch_max_redirects=2, fetch_max_response_bytes=1024)


def mock_client(handler) -> type[httpx.Client]:
    """`httpx.Client` を差し替え、指定ハンドラで応答するクラスを返す。"""

    class _Client(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _Client


class TestFetchPage:
    def test_returns_html_on_success(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert
        assert page.status_code == 200
        assert "hello" in page.html
        assert page.final_url == "https://example.com/a"

    def test_follows_redirect_and_reports_final_url(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a":
                return httpx.Response(301, headers={"location": "/b"})
            return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert — canonical 解決の基準になるため最終 URL を返す
        assert page.final_url == "https://example.com/b"

    def test_rejects_redirect_to_internal_address(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 最初は公開 IP、リダイレクト先だけ内部 IP を返す DNS
        def resolver(host, port, *args, **kwargs):
            address = "127.0.0.1" if host == "internal.example.com" else "93.184.216.34"
            return fake_getaddrinfo(address)(host, port)

        monkeypatch.setattr(socket, "getaddrinfo", resolver)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://internal.example.com/secret"})

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert — リダイレクト先も毎回検証している
        with pytest.raises(UnsafeUrlError, match="到達が禁止された"):
            fetch_page("https://example.com/a", settings=settings)

    def test_rejects_too_many_redirects(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 無限にリダイレクトし続ける
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "/next"})

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(TooManyRedirectsError):
            fetch_page("https://example.com/a", settings=settings)

    def test_rejects_redirect_without_location(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302)

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(FetchError):
            fetch_page("https://example.com/a", settings=settings)

    def test_rejects_response_larger_than_limit(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — Content-Length を偽っても実読み込み量で判定する
        oversized = "a" * (settings.fetch_max_response_bytes + 1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "10"},
                text=oversized,
            )

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(ResponseTooLargeError):
            fetch_page("https://example.com/a", settings=settings)

    @pytest.mark.parametrize(
        "content_type",
        ["application/pdf", "image/png", "application/zip", "application/octet-stream"],
    )
    def test_rejects_non_html_content_types(
        self,
        content_type: str,
        public_dns,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": content_type}, text="x")

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(UnsupportedContentTypeError):
            fetch_page("https://example.com/a", settings=settings)

    def test_rejects_non_http_scheme_before_any_request(self, settings: Settings):
        # Arrange / Act / Assert — 取得を試みる前にスキームで弾く
        with pytest.raises(UnsafeUrlError):
            fetch_page("file:///etc/passwd", settings=settings)

    def test_raises_fetch_error_on_http_error_status(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, headers={"content-type": "text/html"}, text="nope")

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(FetchError, match="404"):
            fetch_page("https://example.com/a", settings=settings)

    def test_does_not_send_cookies_or_credentials(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        fetch_page("https://example.com/a", settings=settings)

        # Assert
        assert "cookie" not in seen
        assert "authorization" not in seen
        assert seen["user-agent"] == settings.fetch_user_agent
