"""SSRF 対策（`PROJECT_SPEC.md` §21）。

外部 URL は完全な非信頼入力として扱う。ホスト名の文字列一致では
`http://[::1]/` や `http://2130706433/` のような表記を取りこぼすため、
**DNS 解決後の IP アドレス**を検証する。

リダイレクト先も毎回同じ検証を通す（呼び出し側は `techradar.fetcher.http` を参照）。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from techradar.fetcher.errors import UnsafeUrlError

ALLOWED_SCHEMES = frozenset({"http", "https"})

# 仕様 §21 で明示された拒否対象に加え、到達すると危険なレンジを含める。
# ipaddress の判定プロパティで拾えないものだけを明示的に列挙する。
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # クラウドのメタデータエンドポイント（リンクローカルに含まれるが意図を明示する）
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
    # 共有アドレス空間（CGN）。外部から到達すべきでない
    ipaddress.ip_network("100.64.0.0/10"),
    # IPv4 射影・変換アドレス経由での回避を防ぐ
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
)


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """到達を禁止すべき IP アドレスかを判定する。

    プライベート・ループバック・リンクローカル・マルチキャストなどは
    `ipaddress` の判定を使い、取りこぼす範囲のみ明示的に列挙する。
    """
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        # IPv6 サイトローカル (fec0::/10)。RFC 3879 で非推奨だが古い社内網では現役で、
        # `is_private` では判定されない。
        or getattr(ip, "is_site_local", False)
    ):
        return True

    # IPv4 射影 IPv6 (::ffff:10.0.0.1) の展開。現行の CPython では上の `is_private` が
    # 既に True を返すため通常は到達しないが、判定が変わっても防御が抜けないよう残す。
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and is_blocked_ip(mapped):
        return True

    return any(ip in network for network in _BLOCKED_NETWORKS)


def validate_scheme(url: str) -> None:
    """HTTP / HTTPS 以外のスキームを拒否する。"""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        message = f"許可されていないスキームです: {scheme or '(なし)'}"
        raise UnsafeUrlError(message)


def resolve_host(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """ホスト名を解決し、得られた全 IP アドレスを返す。"""
    try:
        addrinfo = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        message = f"ホスト名を解決できません: {host}"
        raise UnsafeUrlError(message) from exc

    return [ipaddress.ip_address(info[4][0]) for info in addrinfo]


def validate_url(url: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """URL を検証し、解決された IP アドレスを返す。

    解決結果が**1 つでも**禁止レンジに含まれる場合は拒否する。
    一部だけ許可すると、複数レコードを返す DNS で内部 IP へ到達しうるため。
    """
    validate_scheme(url)

    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        message = "ホスト名がありません"
        raise UnsafeUrlError(message)

    try:
        explicit_port = parts.port
    except ValueError as exc:
        # 範囲外のポート番号。未処理のまま伝播させず、拒否として扱う。
        message = f"ポート番号が不正です: {url}"
        raise UnsafeUrlError(message) from exc

    port = explicit_port or (443 if parts.scheme.lower() == "https" else 80)
    addresses = resolve_host(host, port)
    if not addresses:
        message = f"ホスト名を解決できません: {host}"
        raise UnsafeUrlError(message)

    for address in addresses:
        if is_blocked_ip(address):
            message = f"到達が禁止されたアドレスです: {host} -> {address}"
            raise UnsafeUrlError(message)

    return addresses
