"""URL 正規化（`PROJECT_SPEC.md` §17 重複排除の前提）。

同一記事が別 URL で二重登録されないよう、表記ゆれを吸収した正規形を作る。
副作用を持たない純粋関数として実装し、テストしやすくする。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# 記事の同一性に影響しない計測用パラメータ。
TRACKING_PARAMETER_PREFIXES = ("utm_", "pk_", "mtm_", "matomo_", "ga_", "_hs")
TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "spm",
        "cmpid",
        "campaign_id",
    }
)

DEFAULT_PORTS = {"http": 80, "https": 443}


def is_tracking_parameter(name: str) -> bool:
    """計測用パラメータかを判定する。"""
    lowered = name.lower()
    return lowered in TRACKING_PARAMETERS or lowered.startswith(TRACKING_PARAMETER_PREFIXES)


def normalize_url(url: str) -> str:
    """URL を正規化する。

    - スキームとホストを小文字化する
    - 既定ポート (80 / 443) を除去する
    - 計測用パラメータを除去し、残りをキー順に並べ替える
    - フラグメントを除去する
    - パス末尾のスラッシュを除去する（ルートは維持する）

    クエリの順序違いや計測パラメータの有無だけが異なる URL が
    同じ文字列になることを保証する。
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        # ホストを持たない入力はそのまま返し、判断は呼び出し側に委ねる。
        return url.strip()

    try:
        port = parts.port
    except ValueError:
        # 範囲外のポート番号。ここでは例外にせず、判断は検証側 (`validate_url`) に委ねる。
        return url.strip()

    netloc = host
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    kept = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_parameter(name)
    ]
    query = urlencode(sorted(kept))

    # フラグメントは常に落とす（同一ページ内の位置は記事の同一性に影響しない）。
    return urlunsplit((scheme, netloc, path, query, ""))


def resolve_canonical_url(base_url: str, canonical_href: str | None) -> str:
    """`link rel=canonical` を解決して正規化済みの URL を返す。

    canonical が無い、または解決できない場合は元 URL の正規形を返す。
    canonical が別ホストを指す場合は採用しない（乗っ取りを防ぐため）。
    """
    normalized_base = normalize_url(base_url)
    if not canonical_href:
        return normalized_base

    candidate = normalize_url(urljoin(base_url, canonical_href.strip()))
    if urlsplit(candidate).hostname != urlsplit(normalized_base).hostname:
        return normalized_base
    return candidate
