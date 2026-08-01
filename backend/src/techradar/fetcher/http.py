"""安全性検証付きの HTTP 取得（`PROJECT_SPEC.md` §21）。

httpx の自動リダイレクトは使わない。リダイレクト先が内部 IP を指す攻撃を防ぐため、
**1 ホップごとに** `validate_url` を通してから次の取得を行う。

制限（すべて設定値）:
- 最大リダイレクト回数
- 最大レスポンスサイズ（ストリーミングで読みながら打ち切る）
- 接続・読み取りタイムアウト
- Content-Type（HTML 系のみ許可）

JavaScript は実行しない（HTML を文字列として取得するだけ）。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from techradar.config import Settings, get_settings
from techradar.fetcher.errors import (
    FetchError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
)
from techradar.fetcher.ssrf import validate_url

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class FetchedPage:
    """取得したページ。

    `final_url` はリダイレクトを追い終えた後の URL。canonical 解決の基準に使う。
    """

    final_url: str
    html: str
    content_type: str
    status_code: int


def _check_content_type(content_type: str) -> None:
    """HTML 以外を拒否する。"""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type and not media_type.startswith(ALLOWED_CONTENT_TYPES):
        message = f"HTML ではありません: {media_type}"
        raise UnsupportedContentTypeError(message)


def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """上限バイト数まで読み、超えたら打ち切って例外にする。

    Content-Length を信用せず、実際に読んだ量で判断する。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            message = f"レスポンスが上限 {max_bytes} バイトを超えました"
            raise ResponseTooLargeError(message)
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_page(url: str, settings: Settings | None = None) -> FetchedPage:
    """URL を検証しながら取得する。

    取得前と各リダイレクト先で `validate_url` を通す。
    """
    resolved = settings or get_settings()
    timeout = httpx.Timeout(
        connect=resolved.fetch_connect_timeout_seconds,
        read=resolved.fetch_read_timeout_seconds,
        write=resolved.fetch_connect_timeout_seconds,
        pool=resolved.fetch_connect_timeout_seconds,
    )
    headers = {
        "User-Agent": resolved.fetch_user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.8",
    }

    current_url = url
    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
        # 圧縮爆弾を避けるため自動解凍の対象を絞る。
        http2=False,
    ) as client:
        for _ in range(resolved.fetch_max_redirects + 1):
            # リダイレクト先も含め、毎ホップで検証する。
            validate_url(current_url)

            try:
                with client.stream("GET", current_url) as response:
                    if response.status_code in REDIRECT_STATUS_CODES:
                        location = response.headers.get("location")
                        if not location:
                            message = "リダイレクト先が指定されていません"
                            raise FetchError(message)
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    _check_content_type(content_type)
                    body = _read_limited(response, resolved.fetch_max_response_bytes)
            except httpx.HTTPStatusError as exc:
                message = f"HTTP エラー: {exc.response.status_code}"
                raise FetchError(message) from exc
            except httpx.HTTPError as exc:
                message = f"取得に失敗しました: {exc}"
                raise FetchError(message) from exc

            return FetchedPage(
                final_url=current_url,
                html=body.decode(response.encoding or "utf-8", errors="replace"),
                content_type=content_type,
                status_code=response.status_code,
            )

    message = f"リダイレクトが上限 {resolved.fetch_max_redirects} 回を超えました"
    raise TooManyRedirectsError(message)
