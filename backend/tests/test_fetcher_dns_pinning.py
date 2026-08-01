"""DNS リバインディング (TOCTOU) 対策の接続 pin を確かめる（Issue #21）。

`validate_url` で検証した IP と実際に接続する IP が食い違うと、検証と
接続の間に応答を差し替えられて内部ネットワークへ到達しうる。
`PinnedIPTransport` 単体の振る舞いと、`fetch_page` が正しく pin 情報を
渡して結線されていることの両方を確かめる。

`fetch_page` 経由のテストは `fetcher_http.httpx.HTTPTransport` を差し替える
ことで、実装が構築する `PinnedIPTransport(inner=httpx.HTTPTransport(...))`
の inner 部分だけを `httpx.MockTransport` に置き換える。既存テスト
（`test_fetcher_http.py` の `mock_client`）のように `httpx.Client` 自体を
差し替えると `PinnedIPTransport` を経由しなくなるため、ここでは使わない。
"""

from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest

from techradar.config import Settings
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.errors import UnsafeUrlError
from techradar.fetcher.http import fetch_page
from techradar.fetcher.pinned_transport import PINNED_IP_EXTENSION_KEY, PinnedIPTransport
from tests.test_fetcher_ssrf import fake_getaddrinfo

HTML = "<html lang='en'><head><title>T</title></head><body><p>hello</p></body></html>"


def stub_http_transport_factory(handler):
    """`httpx.HTTPTransport(...)` の呼び出しを `MockTransport` へ差し替える。

    `fetch_page` は `httpx.HTTPTransport(trust_env=False)` を inner として
    `PinnedIPTransport` に渡す。実装がその結線をしていることを込みで検証する
    ため、`httpx.HTTPTransport` 自体をこの factory に差し替える。
    """

    def _factory(*args, **kwargs):
        return httpx.MockTransport(handler)

    return _factory


def html_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, fetch_max_redirects=2, fetch_max_response_bytes=1024)


class TestPinnedIPTransportUnit:
    """`PinnedIPTransport` 単体の振る舞い。"""

    def test_rejects_request_without_pinned_ip(self):
        # Arrange — pin を付け忘れた経路を模す
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request("GET", "https://example.com/a")

        # Act / Assert — fail-closed で拒否し、inner へは到達させない
        with pytest.raises(UnsafeUrlError):
            transport.handle_request(request)
        assert recorded == []

    def test_rewrites_connection_target_while_preserving_original_host(self):
        # Arrange
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request(
            "GET",
            "https://example.com/article",
            extensions={PINNED_IP_EXTENSION_KEY: "93.184.216.34"},
        )

        # Act
        transport.handle_request(request)

        # Assert — 接続先は pin された IP、Host/SNI は元のホスト名
        assert len(recorded) == 1
        sent = recorded[0]
        assert sent.url.host == "93.184.216.34"
        assert sent.headers["Host"] == "example.com"
        assert sent.extensions["sni_hostname"] == "example.com"

    def test_keeps_non_default_port_in_host_header(self):
        # Arrange
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request(
            "GET",
            "https://example.com:8443/article",
            extensions={PINNED_IP_EXTENSION_KEY: "93.184.216.34"},
        )

        # Act
        transport.handle_request(request)

        # Assert
        sent = recorded[0]
        assert sent.url.host == "93.184.216.34"
        assert sent.url.port == 8443
        assert sent.headers["Host"] == "example.com:8443"
        assert sent.extensions["sni_hostname"] == "example.com"

    def test_omits_explicit_default_port_from_host_header(self):
        # Arrange — 明示的に :443 と書かれていても既定ポートなら省く
        # （httpx.URL が構築時に port=None へ正規化するため、この省略は
        # httpx 側の挙動に依存する。ここではその前提が崩れていないことを確認する）
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request(
            "GET",
            "https://example.com:443/article",
            extensions={PINNED_IP_EXTENSION_KEY: "93.184.216.34"},
        )

        # Act
        transport.handle_request(request)

        # Assert
        sent = recorded[0]
        assert sent.headers["Host"] == "example.com"

    def test_supports_ipv6_pinned_address(self):
        # Arrange
        recorded: list[httpx.Request] = []
        ipv6_address = "2606:2800:220:1:248:1893:25c8:1946"

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request(
            "GET",
            "https://example.com/article",
            extensions={PINNED_IP_EXTENSION_KEY: ipv6_address},
        )

        # Act
        transport.handle_request(request)

        # Assert — URL が壊れず、接続先ホストが IPv6 アドレスそのものになる
        sent = recorded[0]
        assert sent.url.host == ipv6_address
        assert str(sent.url).startswith(f"https://[{ipv6_address}]")
        assert sent.headers["Host"] == "example.com"
        assert sent.extensions["sni_hostname"] == "example.com"

    def test_rejects_hostname_as_pinned_value(self):
        # Arrange — pin 値にホスト名文字列が渡るケース（呼び出し側の不備を模す）。
        # IP リテラルでなければ httpcore が再解決してしまい対策が無意味になるため拒否する
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request(
            "GET",
            "https://example.com/article",
            extensions={PINNED_IP_EXTENSION_KEY: "attacker.example.net"},
        )

        # Act / Assert — fail-closed で拒否し、inner へは到達させない
        with pytest.raises(UnsafeUrlError):
            transport.handle_request(request)
        assert recorded == []

    def test_supports_ipv6_pinned_address_with_non_default_port(self):
        # Arrange — IPv6 pin と非既定ポートの組合せ（それぞれ単独ではテスト済みだが未検証）
        recorded: list[httpx.Request] = []
        ipv6_address = "2606:2800:220:1:248:1893:25c8:1946"

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return httpx.Response(200)

        transport = PinnedIPTransport(inner=httpx.MockTransport(handler))
        request = httpx.Request(
            "GET",
            "https://example.com:8443/article",
            extensions={PINNED_IP_EXTENSION_KEY: ipv6_address},
        )

        # Act
        transport.handle_request(request)

        # Assert
        sent = recorded[0]
        assert sent.url.host == ipv6_address
        assert sent.url.port == 8443
        assert str(sent.url).startswith(f"https://[{ipv6_address}]:8443")
        assert sent.headers["Host"] == "example.com:8443"
        assert sent.extensions["sni_hostname"] == "example.com"

    def test_close_delegates_to_inner(self):
        # Arrange
        closed = {"called": False}

        class _RecordingTransport(httpx.MockTransport):
            def close(self) -> None:
                closed["called"] = True
                super().close()

        transport = PinnedIPTransport(inner=_RecordingTransport(html_response))

        # Act
        transport.close()

        # Assert
        assert closed["called"] is True


class TestFetchPageUsesPinnedTransport:
    """`fetch_page` が `PinnedIPTransport` を正しく結線していることを確かめる。"""

    def test_dns_is_resolved_exactly_once_per_hop(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        call_count = {"n": 0}
        base_resolver = fake_getaddrinfo("93.184.216.34")

        def counting_resolver(host, port, *args, **kwargs):
            call_count["n"] += 1
            return base_resolver(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", counting_resolver)
        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(html_response)
        )

        # Act
        fetch_page("https://example.com/a", settings=settings)

        # Assert — 検証用の解決が 1 回だけで、接続時に再解決しない
        assert call_count["n"] == 1

    def test_never_connects_to_ip_returned_by_a_later_resolution(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 1 回目は公開 IP、2 回目以降は内部 IP を返す DNS
        # （リバインディング攻撃を模す）。接続先は常に最初に検証した IP のはず
        call_count = {"n": 0}

        def rebinding_resolver(host, port, *args, **kwargs):
            call_count["n"] += 1
            address = "93.184.216.34" if call_count["n"] == 1 else "127.0.0.1"
            return fake_getaddrinfo(address)(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", rebinding_resolver)

        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return html_response(request)

        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(handler)
        )

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert — 接続先は常に最初に検証した公開 IP。127.0.0.1 へは一度も接続しない
        assert page.status_code == 200
        assert len(recorded) == 1
        assert recorded[0].url.host == "93.184.216.34"
        assert all(r.url.host != "127.0.0.1" for r in recorded)

    def test_https_uses_original_hostname_for_sni_and_certificate_check(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return html_response(request)

        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(handler)
        )

        # Act
        fetch_page("https://example.com/a", settings=settings)

        # Assert — (a) 接続先は pin IP (b) Host は元ホスト名 (c) SNI も元ホスト名
        sent = recorded[0]
        assert sent.url.host == "93.184.216.34"
        assert sent.headers["Host"] == "example.com"
        assert sent.extensions["sni_hostname"] == "example.com"

    def test_pins_ipv6_address_end_to_end(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        ipv6_address = "2606:2800:220:1:248:1893:25c8:1946"
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(ipv6_address))
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return html_response(request)

        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(handler)
        )

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert — URL が壊れず取得できる。final_url は元のホスト名のまま
        assert page.status_code == 200
        assert recorded[0].url.host == ipv6_address
        assert page.final_url == "https://example.com/a"

    def test_pins_each_redirect_hop_to_its_own_host(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — ホップごとに異なるホスト・異なる IP
        def resolver(host, port, *args, **kwargs):
            address = "93.184.216.34" if host == "example.com" else "8.8.8.8"
            return fake_getaddrinfo(address)(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolver)

        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            if request.url.host == "93.184.216.34":
                return httpx.Response(301, headers={"location": "https://second.example.net/b"})
            return html_response(request)

        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(handler)
        )

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert — 1 ホップ目は example.com の IP、2 ホップ目は second.example.net の IP
        assert len(recorded) == 2
        assert recorded[0].url.host == "93.184.216.34"
        assert recorded[0].headers["Host"] == "example.com"
        assert recorded[1].url.host == "8.8.8.8"
        assert recorded[1].headers["Host"] == "second.example.net"
        # final_url はホスト名のまま。canonical 解決の基準に使われるため
        assert page.final_url == "https://second.example.net/b"

    def test_rejects_redirect_to_internal_address_before_connecting(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 既存の SSRF テストと同じシナリオを pinned transport 経由で再確認する
        def resolver(host, port, *args, **kwargs):
            address = "127.0.0.1" if host == "internal.example.com" else "93.184.216.34"
            return fake_getaddrinfo(address)(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", resolver)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://internal.example.com/secret"})

        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(handler)
        )

        # Act / Assert
        with pytest.raises(UnsafeUrlError, match="到達が禁止された"):
            fetch_page("https://example.com/a", settings=settings)

    def test_final_url_keeps_hostname_not_pinned_ip(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(html_response)
        )

        # Act
        page = fetch_page("https://example.com/a", settings=settings)

        # Assert — canonical 解決の基準に使われるため IP 化した URL を返してはいけない
        assert page.final_url == "https://example.com/a"
        assert "93.184.216.34" not in page.final_url


class TestFetchPageDisablesConnectionReuse:
    """コネクションプール再利用によるTLS証明書検証スキップを防ぐ設定を確かめる。

    httpcore の `can_handle_request` は origin（scheme, host, port）の一致だけで
    再利用可否を判定する。PinnedIPTransport は接続先を IP へ書き換えるため、
    origin はホスト名を含まなくなる。SNI・証明書検証は新規接続確立時にしか
    行われないため、異なるホスト名のホップが同一 IP に解決されると、後続の
    ホップが前段のホップ用に確立された TLS 接続（別ホスト向けに検証済み）を
    使い回してしまい、証明書検証が一度も行われなくなる。
    `max_keepalive_connections=0` でコネクション再利用自体を無効化することで
    全ホップで新規接続・新規検証を強制していることを確かめる。
    """

    def test_inner_http_transport_disables_keepalive_and_trust_env(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — httpx.HTTPTransport(...) へ渡される引数を記録する factory に差し替える
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        recorded_kwargs: list[dict] = []

        def factory(*args, **kwargs):
            recorded_kwargs.append(kwargs)
            return httpx.MockTransport(html_response)

        monkeypatch.setattr(fetcher_http.httpx, "HTTPTransport", factory)

        # Act
        fetch_page("https://example.com/a", settings=settings)

        # Assert — コネクション再利用が無効化されており、環境のプロキシ設定も使わない
        assert len(recorded_kwargs) == 1
        kwargs = recorded_kwargs[0]
        assert kwargs["trust_env"] is False
        limits = kwargs["limits"]
        assert isinstance(limits, httpx.Limits)
        assert limits.max_keepalive_connections == 0


class TestPinnedIpExtensionKeyIsPlainAddress:
    def test_pinned_ip_matches_validate_url_result(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange — pin される IP が validate_url の解決結果と一致すること
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            recorded.append(request)
            return html_response(request)

        monkeypatch.setattr(
            fetcher_http.httpx, "HTTPTransport", stub_http_transport_factory(handler)
        )
        settings = Settings(_env_file=None)

        # Act
        fetch_page("https://example.com/a", settings=settings)

        # Assert
        pinned = recorded[0].url.host
        assert ipaddress.ip_address(pinned) == ipaddress.ip_address("93.184.216.34")
