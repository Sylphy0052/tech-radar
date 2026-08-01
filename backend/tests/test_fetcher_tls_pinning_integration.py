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

# `httpx.Client` を経由せず transport を直接叩くため、既定のタイムアウトが
# 適用されない。TLSハンドシェイクが応答しない状況でテストが無限に待つのを避ける。
TIMEOUT_EXTENSION = {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


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
def tls_server(ca: trustme.CA) -> Iterator[str]:
    """`127.0.0.1` のポート0（OS任せ）で待ち受ける実HTTPSサーバを起動する。

    サーバ証明書の identity は `example.com` のみで `127.0.0.1` を含めない。
    実際に割り当てられたポートを `127.0.0.1:<port>` の形式で返す。
    """
    server_cert = ca.issue_cert(SERVER_IDENTITY)
    server_ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_cert.configure_cert(server_ssl_ctx)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    httpd.socket = server_ssl_ctx.wrap_socket(httpd.socket, server_side=True)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        yield f"127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def pinned_ip() -> str:
    return "127.0.0.1"


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
        self, ca: trustme.CA, tls_server: str, pinned_ip: str
    ):
        # Arrange — サーバ証明書の identity は `example.com` のみ（`127.0.0.1` の
        # IP SANは無い）。pin した IP に対して検証していれば必ず失敗するはず
        _, port = tls_server.split(":")
        transport = _build_transport(ca)
        request = httpx.Request(
            "GET",
            f"https://{SERVER_IDENTITY}:{port}/",
            extensions={PINNED_IP_EXTENSION_KEY: pinned_ip, "timeout": TIMEOUT_EXTENSION},
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
        self, ca: trustme.CA, tls_server: str, pinned_ip: str
    ):
        # Arrange — 証明書のidentity（`example.com`）と一致しないホスト名で
        # 要求する。SNI・証明書検証が正しく機能していれば拒否されるはず
        _, port = tls_server.split(":")
        transport = _build_transport(ca)
        request = httpx.Request(
            "GET",
            f"https://wrong.example.org:{port}/",
            extensions={PINNED_IP_EXTENSION_KEY: pinned_ip, "timeout": TIMEOUT_EXTENSION},
        )

        # Act / Assert — httpx は下位の ssl.SSLCertVerificationError を
        # httpx.ConnectError にラップする。ホスト名不一致がメッセージに現れることも確認する
        try:
            with pytest.raises(httpx.ConnectError, match=re.escape("wrong.example.org")):
                transport.handle_request(request)
        finally:
            transport.close()
