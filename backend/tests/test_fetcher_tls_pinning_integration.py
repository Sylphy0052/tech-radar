"""`PinnedIPTransport` の SNI/証明書検証を実TLSハンドシェイクで裏取りする（Issue #22）。

`test_fetcher_dns_pinning.py` は `httpx.MockTransport` で
`request.extensions["sni_hostname"]` に値が渡ることまでしか確認していない。
この extension を httpcore が実際にどう扱うか（`ssl_context.wrap_socket(
server_hostname=...)` に渡し、SNI と証明書検証を「pin した IP」ではなく
「元のホスト名」に対して行う）はソース読解でしか裏取りされておらず、
httpcore の非公開挙動が変わっても検出できない。

このテストは `trustme` で `example.com` のみを identity とする証明書を発行し
（`127.0.0.1` は IP SAN に含めない）、`127.0.0.1` で待ち受ける実TLSサーバへ
`PinnedIPTransport` 経由で接続する。もし証明書検証が pin した IP に対して
行われていれば、IP SAN が無いため接続は必ず失敗する。したがって「接続が
成功すること」自体が「検証が元のホスト名に対して行われていること」の証拠になる。
"""

from __future__ import annotations

import re
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import trustme

from techradar.fetcher.pinned_transport import PINNED_IP_EXTENSION_KEY, PinnedIPTransport

SERVER_IDENTITY = "example.com"

# サーバ証明書の identity と一致しないホスト名。異常系で SNI を誤らせるために使う。
MISMATCHED_HOSTNAME = "wrong.example.org"

# テストサーバの bind 先であり、そのまま pin する IP でもある。両者がずれると
# 「pin した IP へ接続している」という前提が崩れるため、一箇所で定義する。
LOOPBACK_IP = "127.0.0.1"

# `httpx.Client` を経由せず transport を直接叩くため、既定のタイムアウトが
# 適用されない。TLSハンドシェイクが応答しない状況でテストが無限に待つのを避ける。
TIMEOUT_EXTENSION = {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}

SERVER_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """例外の連鎖を列挙する。

    実測した連鎖は `httpx.ConnectError` -> (`__cause__`) ->
    `httpcore.ConnectError` -> (`__context__`) -> `ssl.SSLCertVerificationError`
    で、明示的な `raise ... from` (`__cause__`) と暗黙の連鎖 (`__context__`) が
    混在する。段数も繋ぎ方も httpx/httpcore の実装詳細なので決め打ちせず、
    両方を辿って連鎖全体を見る。
    """
    chain: list[BaseException] = []
    current = exc.__cause__ or exc.__context__
    while current is not None and not any(current is seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


class _OkHandler(BaseHTTPRequestHandler):
    """常に200を返すだけの最小ハンドラ。TLS層の検証が本題のためbodyは問わない。"""

    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # テスト出力を汚さないよう、http.server標準のアクセスログを抑止する
        pass


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def tls_server(ca: trustme.CA) -> Iterator[int]:
    """`LOOPBACK_IP` のポート0（OS任せ）で待ち受ける実HTTPSサーバを起動する。

    サーバ証明書の identity は `example.com` のみで、bind 先の IP は含めない。
    実際に割り当てられたポート番号を返す。
    """
    server_cert = ca.issue_cert(SERVER_IDENTITY)
    server_ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(server_ssl_ctx)

    httpd = ThreadingHTTPServer((LOOPBACK_IP, 0), _OkHandler)
    httpd.socket = server_ssl_ctx.wrap_socket(httpd.socket, server_side=True)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS)
        # join のタイムアウトを黙って見逃すと、停止しないサーバが後続テストへ
        # 影響してもテストは通り続ける。停止したことを明示的に検証する。
        assert not thread.is_alive(), "TLSサーバのスレッドが停止しなかった"


def _build_transport(ca: trustme.CA) -> PinnedIPTransport:
    """CAを信頼したクライアントcontextを持つ`PinnedIPTransport`を組み立てる。"""
    client_ssl_ctx = ssl.create_default_context()
    ca.configure_trust(client_ssl_ctx)
    inner = httpx.HTTPTransport(verify=client_ssl_ctx)
    return PinnedIPTransport(inner=inner)


class TestCertificateVerifiedAgainstOriginalHostname:
    """`PinnedIPTransport` が SNI・証明書検証を pin した IP ではなく
    元のホスト名に対して行うことを確かめる。
    """

    def test_certificate_is_verified_against_original_hostname_not_pinned_ip(
        self, ca: trustme.CA, tls_server: int
    ):
        # Arrange — サーバ証明書の identity は `example.com` のみ（bind 先の IP の
        # IP SANは無い）。pin した IP に対して検証していれば必ず失敗するはず
        transport = _build_transport(ca)
        request = httpx.Request(
            "GET",
            f"https://{SERVER_IDENTITY}:{tls_server}/",
            extensions={PINNED_IP_EXTENSION_KEY: LOOPBACK_IP, "timeout": TIMEOUT_EXTENSION},
        )

        # Act
        try:
            response = transport.handle_request(request)
            response.read()
        finally:
            transport.close()

        # Assert — 接続・証明書検証が成功し、実TLSハンドシェイクを通過している
        assert response.status_code == 200
        assert response.text == "ok"

    def test_mismatched_sni_hostname_fails_certificate_verification(
        self, ca: trustme.CA, tls_server: int
    ):
        # Arrange — 証明書のidentity（`example.com`）と一致しないホスト名で
        # 要求する。SNI・証明書検証が正しく機能していれば拒否されるはず
        transport = _build_transport(ca)
        request = httpx.Request(
            "GET",
            f"https://{MISMATCHED_HOSTNAME}:{tls_server}/",
            extensions={PINNED_IP_EXTENSION_KEY: LOOPBACK_IP, "timeout": TIMEOUT_EXTENSION},
        )

        # Act — httpx は下位の ssl.SSLCertVerificationError を
        # httpx.ConnectError にラップする
        try:
            with pytest.raises(httpx.ConnectError, match=re.escape(MISMATCHED_HOSTNAME)) as exc:
                transport.handle_request(request)
        finally:
            transport.close()

        # Assert — 失敗の理由が「要求したホスト名に対する証明書検証」であることを
        # 二重に確かめる。メッセージ一致は SNI が pin した IP ではなく元のホスト名で
        # 渡っている証拠になり（pin した IP が使われていればメッセージは IP になる）、
        # 例外連鎖の型チェックは OpenSSL のメッセージ表現が将来変わっても残る。
        assert any(
            isinstance(cause, ssl.SSLCertVerificationError) for cause in _exception_chain(exc.value)
        )
