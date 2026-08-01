"""SSRF 対策を検証する（`PROJECT_SPEC.md` §21）。

実際の名前解決に依存しないよう `socket.getaddrinfo` をモックし、
「DNS が内部 IP を返す」状況を再現して拒否されることを確かめる。
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from techradar.fetcher.errors import UnsafeUrlError
from techradar.fetcher.ssrf import is_blocked_ip, validate_scheme, validate_url


def fake_getaddrinfo(*addresses: str):
    """指定の IP を返す `getaddrinfo` の代替を作る。"""

    def _resolver(host, port, *args, **kwargs):
        infos = []
        for address in addresses:
            ip = ipaddress.ip_address(address)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            sockaddr = (address, port, 0, 0) if ip.version == 6 else (address, port)
            infos.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return infos

    return _resolver


# 仕様 §21 に列挙された拒否対象を網羅する。
BLOCKED_ADDRESSES = [
    pytest.param("127.0.0.1", id="ipv4-loopback"),
    pytest.param("127.10.20.30", id="ipv4-loopback-range"),
    pytest.param("10.0.0.1", id="ipv4-private-10"),
    pytest.param("172.16.0.1", id="ipv4-private-172-16"),
    pytest.param("172.31.255.254", id="ipv4-private-172-31"),
    pytest.param("192.168.1.1", id="ipv4-private-192-168"),
    pytest.param("169.254.1.1", id="ipv4-link-local"),
    pytest.param("169.254.169.254", id="cloud-metadata"),
    pytest.param("0.0.0.0", id="ipv4-unspecified"),  # noqa: S104
    pytest.param("100.64.0.1", id="carrier-grade-nat"),
    pytest.param("::1", id="ipv6-loopback"),
    pytest.param("fd00::1", id="ipv6-unique-local"),
    pytest.param("fe80::1", id="ipv6-link-local"),
    pytest.param("fd00:ec2::254", id="ipv6-cloud-metadata"),
    pytest.param("::ffff:127.0.0.1", id="ipv4-mapped-loopback"),
    pytest.param("::ffff:10.0.0.1", id="ipv4-mapped-private"),
    pytest.param("::", id="ipv6-unspecified"),
    pytest.param("224.0.0.1", id="ipv4-multicast"),
]

PUBLIC_ADDRESSES = [
    pytest.param("93.184.216.34", id="ipv4-public"),
    pytest.param("2606:2800:220:1:248:1893:25c8:1946", id="ipv6-public"),
]


class TestIsBlockedIp:
    @pytest.mark.parametrize("address", BLOCKED_ADDRESSES)
    def test_blocks_internal_ranges(self, address: str):
        # Arrange / Act / Assert
        assert is_blocked_ip(ipaddress.ip_address(address)) is True

    @pytest.mark.parametrize("address", PUBLIC_ADDRESSES)
    def test_allows_public_addresses(self, address: str):
        # Arrange / Act / Assert
        assert is_blocked_ip(ipaddress.ip_address(address)) is False


class TestValidateScheme:
    @pytest.mark.parametrize("url", ["http://example.com", "https://example.com"])
    def test_allows_http_and_https(self, url: str):
        # Arrange / Act / Assert
        validate_scheme(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com/",
            "data:text/html,<script>alert(1)</script>",
            "javascript:alert(1)",
            "//example.com/x",
        ],
    )
    def test_rejects_other_schemes(self, url: str):
        # Arrange / Act / Assert
        with pytest.raises(UnsafeUrlError):
            validate_scheme(url)


class TestValidateUrl:
    @pytest.mark.parametrize("address", BLOCKED_ADDRESSES)
    def test_rejects_hosts_resolving_to_internal_ips(
        self, address: str, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 公開ドメインに見えて内部 IP を返す DNS を再現する
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo(address))

        # Act / Assert
        with pytest.raises(UnsafeUrlError, match="到達が禁止された"):
            validate_url("https://innocent-looking.example.com/article")

    def test_allows_public_addresses(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))

        # Act
        addresses = validate_url("https://example.com/article")

        # Assert
        assert addresses == [ipaddress.ip_address("93.184.216.34")]

    def test_rejects_when_any_resolved_address_is_internal(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange — 公開 IP と内部 IP を混ぜて返す DNS。1 つでも内部なら拒否する
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34", "127.0.0.1"))

        # Act / Assert
        with pytest.raises(UnsafeUrlError, match="到達が禁止された"):
            validate_url("https://mixed.example.com/article")

    def test_rejects_decimal_encoded_loopback(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange — 2130706433 は 127.0.0.1 の10進表記。
        # 文字列一致では防げないため、解決結果で判定していることを確認する
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("127.0.0.1"))

        # Act / Assert
        with pytest.raises(UnsafeUrlError):
            validate_url("http://2130706433/")

    def test_rejects_unresolvable_host(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange
        def _raise(*args, **kwargs):
            raise socket.gaierror("not found")

        monkeypatch.setattr(socket, "getaddrinfo", _raise)

        # Act / Assert
        with pytest.raises(UnsafeUrlError, match="解決できません"):
            validate_url("https://does-not-exist.example.com/")

    def test_rejects_url_without_host(self):
        # Arrange / Act / Assert
        with pytest.raises(UnsafeUrlError):
            validate_url("https:///path-only")
