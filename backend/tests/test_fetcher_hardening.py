"""レビューで判明した回避経路・未検証パスを固定するテスト。

いずれも「一見防げているが実際は抜けられる」ケースなので、
仕様変更で緩んだときに気づけるよう独立したファイルにまとめる。
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import UTC

import httpx
import pytest

from techradar.config import Settings
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.errors import (
    ExtractionError,
    FetchError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
)
from techradar.fetcher.extract import _parse_published_at as parse_published_at
from techradar.fetcher.extract import (
    extract_article,
    has_dangerous_scheme,
    sanitize_html,
)
from techradar.fetcher.http import decode_body, fetch_page
from techradar.fetcher.ssrf import is_blocked_ip, validate_url
from techradar.fetcher.url import normalize_url
from tests.test_fetcher_extract import article_html
from tests.test_fetcher_http import mock_client
from tests.test_fetcher_ssrf import fake_getaddrinfo


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class TestSiteLocalAddresses:
    @pytest.mark.parametrize("address", ["fec0::1", "fec0::c0a8:101", "feff::1"])
    def test_blocks_deprecated_ipv6_site_local(self, address: str):
        # Arrange / Act / Assert — RFC 3879 で非推奨だが社内網では現役。
        # `is_private` では判定されないため個別に確認する
        assert is_blocked_ip(ipaddress.ip_address(address)) is True

    def test_rejects_host_resolving_to_site_local(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("fec0::1"))

        # Act / Assert
        with pytest.raises(UnsafeUrlError, match="到達が禁止された"):
            validate_url("https://looks-public.example.com/article")


class TestMalformedPort:
    def test_validate_url_rejects_out_of_range_port(self):
        # Arrange / Act / Assert — 未処理の ValueError で落ちないこと
        with pytest.raises(UnsafeUrlError, match="ポート番号"):
            validate_url("https://example.com:99999/article")

    def test_normalize_url_survives_out_of_range_port(self):
        # Arrange / Act — 正規化は例外を投げず、判断は検証側へ委ねる
        normalized = normalize_url("https://example.com:99999/article")

        # Assert
        assert normalized == "https://example.com:99999/article"


class TestContentTypeIsAllowlisted:
    def test_rejects_missing_content_type(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — Content-Type を返さないサーバー
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html><body>x</body></html>")

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert — 未指定を素通しすると HTML 限定の制約が回避できてしまう
        with pytest.raises(UnsupportedContentTypeError):
            fetch_page("https://example.com/a", settings=settings)


class TestDecodeBody:
    def test_uses_charset_from_content_type(self):
        # Arrange
        body = "<html><body>日本語の記事</body></html>".encode("shift_jis")

        # Act
        decoded = decode_body(body, "text/html; charset=Shift_JIS")

        # Assert
        assert "日本語の記事" in decoded

    def test_detects_charset_when_header_omits_it(self):
        # Arrange — charset 未指定でも UTF-8 決め打ちにせず推定する
        body = "<html><body>" + ("日本語の記事です。" * 20) + "</body></html>"

        # Act
        decoded = decode_body(body.encode("euc_jp"), "text/html")

        # Assert
        assert "日本語の記事です" in decoded

    def test_falls_back_when_charset_name_is_unknown(self):
        # Arrange
        body = b"<html><body>hello</body></html>"

        # Act
        decoded = decode_body(body, "text/html; charset=x-unknown-charset")

        # Assert
        assert "hello" in decoded

    def test_decodes_shift_jis_page_end_to_end(
        self, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — ストリーミング取得でも文字化けしないこと
        html = article_html().replace("<head>", '<head><meta charset="shift_jis">')

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=Shift_JIS"},
                content=html.encode("shift_jis"),
            )

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert
        assert "Model Context Protocol" in page.html
        assert "MCP サーバー実装ガイド" in page.html


class TestDangerousSchemes:
    @pytest.mark.parametrize(
        "value",
        [
            "javascript:alert(1)",
            "  javascript:alert(1)",
            "JavaScript:alert(1)",
            "jav\tascript:alert(1)",
            "jav\nascript:alert(1)",
            "vbscript:msgbox(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
        ],
    )
    def test_detects_dangerous_schemes(self, value: str):
        # Arrange / Act / Assert — ブラウザは URL 中の制御文字を無視して解釈する
        assert has_dangerous_scheme(value) is True

    @pytest.mark.parametrize(
        "value",
        ["https://example.com/a", "/relative/path", "#anchor", "mailto:a@example.com"],
    )
    def test_keeps_ordinary_urls(self, value: str):
        # Arrange / Act / Assert
        assert has_dangerous_scheme(value) is False

    def test_removes_obfuscated_javascript_url(self):
        # Arrange / Act
        sanitized = sanitize_html(
            '<html><body><a href="jav&#9;ascript:alert(1)">x</a></body></html>'
        )

        # Assert
        assert "alert(1)" not in sanitized

    @pytest.mark.parametrize(
        "dangerous",
        [
            '<base href="https://evil.example.net/">',
            "<style>@import url(https://evil.example.net/x.css);</style>",
        ],
    )
    def test_removes_base_and_style(self, dangerous: str):
        # Arrange / Act
        sanitized = sanitize_html(f"<html><head>{dangerous}</head><body><p>safe</p></body></html>")

        # Assert
        assert "evil.example.net" not in sanitized
        assert "safe" in sanitized


class TestPublishedAtParsing:
    @pytest.mark.parametrize(
        ("value", "expected_year"),
        [
            ("2026-07-28T09:30:00Z", 2026),
            ("2026-07-28T09:30:00+09:00", 2026),
            ("2026-07-28", 2026),
            ("2026-07-28 09:30:00", 2026),
        ],
    )
    def test_parses_common_formats_as_utc(self, value: str, expected_year: int):
        # Arrange / Act
        parsed = parse_published_at(value)

        # Assert — 7 日フィルターの計算がタイムゾーンに依存しないよう UTC に揃える
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.astimezone(UTC).year == expected_year

    @pytest.mark.parametrize("value", [None, "", "いつか", "not-a-date"])
    def test_returns_none_for_unparsable_values(self, value: str | None):
        # Arrange / Act / Assert
        assert parse_published_at(value) is None


class TestExtractionFailures:
    def test_raises_when_no_title_can_be_found(self):
        # Arrange — 本文はあるがタイトル候補が一切ない
        body = "これは十分な長さを持つ本文です。" * 20
        html = f"<html lang='ja'><head></head><body><article><p>{body}</p></article></body></html>"

        # Act / Assert
        with pytest.raises(ExtractionError, match="タイトル"):
            extract_article(html, "https://example.com/no-title")

    def test_falls_back_to_readability_when_trafilatura_returns_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — trafilatura が本文を返さない状況を作る
        monkeypatch.setattr("techradar.fetcher.extract.trafilatura.extract", lambda *a, **k: None)

        # Act — フォールバックが働けば抽出できる
        extracted = extract_article(article_html(), "https://example.com/posts/1")

        # Assert
        assert "Model Context Protocol" in extracted.body


class TestTransportFailures:
    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectTimeout("connect timed out"),
        ],
    )
    def test_converts_transport_errors_to_fetch_error(
        self,
        error: httpx.HTTPError,
        public_dns,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Arrange — 実運用で最も起きやすい失敗系
        def handler(request: httpx.Request) -> httpx.Response:
            raise error

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))

        # Act / Assert — 呼び出し側が扱える型に変換されること
        with pytest.raises(FetchError):
            fetch_page("https://example.com/a", settings=settings)
