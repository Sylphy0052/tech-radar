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

import time
from dataclasses import dataclass
from urllib.parse import urljoin

import charset_normalizer
import httpx

from techradar.config import Settings, get_settings
from techradar.fetcher.errors import (
    FetchError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
)
from techradar.fetcher.pinned_transport import PINNED_IP_EXTENSION_KEY, PinnedIPTransport
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
    """HTML 以外を拒否する。

    許可リスト方式にする。Content-Type を返さないサーバーの応答を
    素通しすると、HTML 限定という制約自体が回避できてしまうため、
    未指定も拒否する。
    """
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not media_type.startswith(ALLOWED_CONTENT_TYPES):
        message = f"HTML ではありません: {media_type or '(Content-Type 未指定)'}"
        raise UnsupportedContentTypeError(message)


def _charset_from_content_type(content_type: str) -> str | None:
    """Content-Type ヘッダの charset パラメータを取り出す。"""
    for parameter in content_type.split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip("\"'")
            if charset:
                return charset
    return None


def decode_body(body: bytes, content_type: str) -> str:
    """レスポンス本文を文字列へ復号する。

    `httpx` の `Response.encoding` は charset 未指定時に UTF-8 へ落ちるため、
    Shift_JIS などで書かれた記事が黙って文字化けする。
    ヘッダの charset を優先し、無ければ本文から推定する。
    """
    charset = _charset_from_content_type(content_type)
    if charset:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            # 未知の charset 名。推定にフォールバックする。
            pass

    detected = charset_normalizer.from_bytes(body).best()
    if detected is not None:
        return str(detected)
    return body.decode("utf-8", errors="replace")


def _read_limited(response: httpx.Response, max_bytes: int, deadline: float) -> bytes:
    """上限バイト数まで読み、超えたら打ち切って例外にする。

    Content-Length を信用せず、実際に読んだ量で判断する。
    上限未満のまま少しずつ送り続けられて接続を占有されないよう、
    総経過時間でも打ち切る。
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            message = f"レスポンスが上限 {max_bytes} バイトを超えました"
            raise ResponseTooLargeError(message)
        if time.monotonic() > deadline:
            message = "レスポンスの読み取りが制限時間を超えました"
            raise FetchError(message)
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

    # ホップ単位のタイムアウトだけでは、少量ずつ送り続けられると接続が長時間残る。
    # 取得全体の締切を設けて確実に打ち切る。
    deadline = time.monotonic() + resolved.fetch_total_timeout_seconds

    current_url = url
    with httpx.Client(
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
        http2=False,
        # 環境変数のプロキシ設定を使わない。プロキシ経由になると実際の接続先が
        # プロキシ側の判断に委ねられ、こちらの IP 検証が意味を失うため。
        trust_env=False,
        # 検証した IP へ直接接続するため実接続は PinnedIPTransport 経由にする。
        # httpx が接続時に独自に再解決すると、検証と接続の間に応答を差し替える
        # DNS リバインディング（TOCTOU）を許してしまう。
        #
        # max_keepalive_connections=0 でコネクション再利用を無効化する。
        # PinnedIPTransport が接続先を IP へ書き換えるため、httpcore の
        # コネクション再利用可否判定（origin 一致）は IP 単位でしか見なくなり、
        # ホスト名の違いを区別しない。TLS の SNI・証明書検証は新規接続確立時に
        # 一度だけ行われるため、リダイレクトのホップ1（ホストA）とホップ2
        # （ホストB）が同一 IP に解決されると、ホップ2 がホップ1 の TLS 接続
        # （SNI=A で検証済み）を再利用してしまい、B に対する証明書検証が
        # 一度も行われなくなる。共有 CDN やマルチテナント環境で実際に成立する
        # 穴のため、コネクション再利用自体を無効化して全ホップで確実に
        # 新規接続・新規検証させる。ホップ数は fetch_max_redirects で
        # 上限が抑えられており、都度接続確立するコストは軽微。
        transport=PinnedIPTransport(
            inner=httpx.HTTPTransport(
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        ),
    ) as client:
        for _ in range(resolved.fetch_max_redirects + 1):
            if time.monotonic() > deadline:
                message = "取得が制限時間を超えました"
                raise FetchError(message)

            # リダイレクト先も含め、毎ホップで検証する。
            # 検証で得た IP のうち先頭 1 つだけを pin する。フォールバックはしない
            # （複数 IP を渡すと httpcore が独自に選び直し、検証済みでない IP へ
            # 接続する余地が生まれるため）。
            addresses = validate_url(current_url)
            pinned_ip = str(addresses[0])

            try:
                with client.stream(
                    "GET",
                    current_url,
                    extensions={PINNED_IP_EXTENSION_KEY: pinned_ip},
                ) as response:
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
                    body = _read_limited(response, resolved.fetch_max_response_bytes, deadline)
                    status_code = response.status_code
            except httpx.HTTPStatusError as exc:
                message = f"HTTP エラー: {exc.response.status_code}"
                raise FetchError(message) from exc
            except httpx.HTTPError as exc:
                message = f"取得に失敗しました: {exc}"
                raise FetchError(message) from exc

            return FetchedPage(
                final_url=current_url,
                html=decode_body(body, content_type),
                content_type=content_type,
                status_code=status_code,
            )

    message = f"リダイレクトが上限 {resolved.fetch_max_redirects} 回を超えました"
    raise TooManyRedirectsError(message)
