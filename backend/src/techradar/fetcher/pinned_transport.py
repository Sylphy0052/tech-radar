"""DNS リバインディング (TOCTOU) 対策の接続 pin（`PROJECT_SPEC.md` §21 / Issue #21）。

`validate_url` は DNS 解決した IP アドレスを検証するが、httpx は接続時に
**独自にもう一度**名前解決する。検証と接続の間に応答を内部 IP へ
差し替えられると、検証をすり抜けて内部ネットワークへ到達できてしまう
（TOCTOU: Time-Of-Check to Time-Of-Use）。

`PinnedIPTransport` はこの隙間を塞ぐ。呼び出し側が検証済みの IP を
`request.extensions` 経由で渡し、このトランスポートは名前解決をやり直さず
その IP へ直接接続する。TLS の SNI と証明書検証は元のホスト名に対して行う。
"""

from __future__ import annotations

import httpx

from techradar.fetcher.errors import UnsafeUrlError

# `request.extensions` に載せる pin 済み IP のキー名。
PINNED_IP_EXTENSION_KEY = "pinned_ip"


def _build_host_header(url: httpx.URL) -> str:
    """`Host` ヘッダの値を組み立てる。

    非既定ポートの場合は `host:port` 形式を維持する。既定ポートは
    `httpx.URL` が構築時点で `port=None` に正規化するため、ここでの
    既定ポート判定は不要（`https://example.com:443/` でも `url.port` は
    `None` になる）。ホスト自体が IPv6 リテラルの場合は角括弧で囲む
    （稀だが正しさのため）。
    """
    host = f"[{url.host}]" if ":" in url.host else url.host
    if url.port is None:
        return host
    return f"{host}:{url.port}"


class PinnedIPTransport(httpx.BaseTransport):
    """検証済み IP への接続に固定する httpx トランスポート。

    ステートレス。pin 情報のような可変状態は持たず、リクエストごとに
    `request.extensions[PINNED_IP_EXTENSION_KEY]` から読み取る。

    pin が付いていないリクエストは fail-closed で拒否する
    （`UnsafeUrlError`）。呼び出し側が pin を付け忘れた経路をそのまま
    通してしまうと、対策自体が構造的に無意味になるため。
    """

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        pinned_ip = request.extensions.get(PINNED_IP_EXTENSION_KEY)
        if not pinned_ip:
            message = f"接続先 IP が pin されていないリクエストです: {request.url}"
            raise UnsafeUrlError(message)

        original_url = request.url
        original_host = original_url.host
        host_header = _build_host_header(original_url)

        pinned_url = original_url.copy_with(host=pinned_ip)

        headers = request.headers.copy()
        headers["host"] = host_header

        # httpcore 1.0.9 の `_sync/connection.py` はこの extension を
        # `ssl_context.wrap_socket(server_hostname=...)` に渡す。
        # SNI と証明書検証を元のホスト名に対して行うために必須。
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = original_host

        pinned_request = httpx.Request(
            method=request.method,
            url=pinned_url,
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return self._inner.handle_request(pinned_request)

    def close(self) -> None:
        self._inner.close()
