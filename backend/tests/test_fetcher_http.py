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
    InvalidHeaderError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
)
from techradar.fetcher.http import (
    FEED_CONTENT_TYPES,
    JSON_CONTENT_TYPES,
    fetch_page,
    fetch_resource,
)
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


class TestFetchResource:
    """`fetch_resource` が HTML 以外の Content-Type でも安全経路を通ることを確かめる。"""

    def test_fetches_feed_content_type_when_allowlisted(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        feed = "<?xml version='1.0'?><rss><channel><title>T</title></channel></rss>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=feed)

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        resource = fetch_resource(
            "https://example.com/feed.xml",
            allowed_content_types=FEED_CONTENT_TYPES,
            settings=settings,
        )

        # Assert
        assert resource.status_code == 200
        assert "<rss>" in resource.text
        assert resource.content_type == "application/rss+xml"

    def test_fetch_page_still_rejects_feed_content_type(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — HTML 限定の `fetch_page` は後退していないこと
        feed = "<?xml version='1.0'?><rss><channel><title>T</title></channel></rss>"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=feed)

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(UnsupportedContentTypeError):
            fetch_page("https://example.com/feed.xml", settings=settings)

    def test_fetches_json_content_type_and_returns_raw_bytes(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        payload = b'{"hits": [{"id": 1}]}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=payload
            )

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        resource = fetch_resource(
            "https://example.com/api.json",
            allowed_content_types=JSON_CONTENT_TYPES,
            settings=settings,
        )

        # Assert — body はバイト列のまま。JSON パース時に文字化け推定を挟まないため
        assert resource.body == payload
        assert resource.content_type == "application/json"

    def test_rejects_missing_content_type_regardless_of_allowlist(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — Content-Type を返さないサーバー
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b'{"hits": []}')

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert — 未指定を素通しすると許可リストという制約自体が回避できてしまう
        with pytest.raises(UnsupportedContentTypeError):
            fetch_resource(
                "https://example.com/api.json",
                allowed_content_types=JSON_CONTENT_TYPES,
                settings=settings,
            )

    def test_rejects_redirect_to_internal_address(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 最初は公開 IP、リダイレクト先だけ内部 IP を返す DNS
        def resolver(host, port, *args, **kwargs):
            address = "127.0.0.1" if host == "internal.example.com" else "93.184.216.34"
            return fake_getaddrinfo(address)(host, port)

        monkeypatch.setattr(socket, "getaddrinfo", resolver)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://internal.example.com/feed"})

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert — フィード/JSON 経路でも毎ホップで検証している
        with pytest.raises(UnsafeUrlError, match="到達が禁止された"):
            fetch_resource(
                "https://example.com/feed.xml",
                allowed_content_types=FEED_CONTENT_TYPES,
                settings=settings,
            )

    def test_rejects_response_larger_than_limit(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — Content-Length を偽っても実読み込み量で判定する
        oversized = "a" * (settings.fetch_max_response_bytes + 1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": "10"},
                text=oversized,
            )

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert
        with pytest.raises(ResponseTooLargeError):
            fetch_resource(
                "https://example.com/api.json",
                allowed_content_types=JSON_CONTENT_TYPES,
                settings=settings,
            )


class TestFetchResourceHeaders:
    """`fetch_resource` の `headers` 引数（Issue #9 T16）を検証する。"""

    def test_sends_custom_headers_on_initial_request(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        fetch_resource(
            "https://example.com/api.json",
            allowed_content_types=JSON_CONTENT_TYPES,
            headers={"X-Subscription-Token": "secret-token"},
            settings=settings,
        )

        # Assert
        assert seen["x-subscription-token"] == "secret-token"

    def test_forwards_headers_across_same_origin_redirect(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — scheme + host + port が一致するリダイレクト先には引き継ぐ
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/a":
                return httpx.Response(301, headers={"location": "/b"})
            seen.update(request.headers)
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        fetch_resource(
            "https://example.com/a",
            allowed_content_types=JSON_CONTENT_TYPES,
            headers={"X-Subscription-Token": "secret-token"},
            settings=settings,
        )

        # Assert
        assert seen["x-subscription-token"] == "secret-token"

    def test_drops_headers_on_cross_origin_redirect(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 別ホストへのリダイレクト先には認証ヘッダ等を送らない
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "example.com":
                return httpx.Response(301, headers={"location": "https://other.example.com/b"})
            seen.update(request.headers)
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        fetch_resource(
            "https://example.com/a",
            allowed_content_types=JSON_CONTENT_TYPES,
            headers={"X-Subscription-Token": "secret-token"},
            settings=settings,
        )

        # Assert — 基本ヘッダ（User-Agent 等）は届くが、追加ヘッダは落ちている
        assert "x-subscription-token" not in seen
        assert seen["user-agent"] == settings.fetch_user_agent

    def test_rejects_header_value_containing_crlf(self, settings: Settings):
        # Arrange / Act / Assert — ヘッダインジェクション対策。リクエスト前に拒否する
        with pytest.raises(InvalidHeaderError):
            fetch_resource(
                "https://example.com/a",
                allowed_content_types=JSON_CONTENT_TYPES,
                headers={"X-Foo": "bar\r\nX-Injected: evil"},
                settings=settings,
            )

    def test_rejects_header_name_containing_crlf(self, settings: Settings):
        # Arrange / Act / Assert
        with pytest.raises(InvalidHeaderError):
            fetch_resource(
                "https://example.com/a",
                allowed_content_types=JSON_CONTENT_TYPES,
                headers={"X-Foo\r\nX-Injected": "bar"},
                settings=settings,
            )

    def test_rejects_host_header_override(self, settings: Settings):
        # Arrange / Act / Assert — Host 上書きは拒否する（無視ではなく明示的エラー）
        with pytest.raises(InvalidHeaderError):
            fetch_resource(
                "https://example.com/a",
                allowed_content_types=JSON_CONTENT_TYPES,
                headers={"Host": "internal.example.com"},
                settings=settings,
            )


class TestFetchResourceIntegration:
    """スタブ化せず `fetch_resource` を実際に呼び出す結合的なテスト。

    ヘッダ引数の呼び出しシグネチャ不一致は、コレクター側のテストが
    `fetch_resource` をスタブ化していると検出できない（このタスクの
    起点になったバグそのもの）。ここでは httpx のトランスポート層だけを
    モックし、キーワード引数としての `headers` が実際の呼び出しで
    受理されることを担保する。
    """

    def test_calls_fetch_resource_with_headers_kwarg_without_typeerror(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=b'{"ok": true}'
            )

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act — brave.py / github_releases.py が実際に行うのと同じ通常呼び出し
        resource = fetch_resource(
            "https://example.com/a",
            allowed_content_types=JSON_CONTENT_TYPES,
            headers={"Authorization": "Bearer secret-token"},
            settings=settings,
        )

        # Assert
        assert resource.status_code == 200
        assert seen["authorization"] == "Bearer secret-token"
