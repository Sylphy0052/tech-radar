"""記事取得で発生するエラー。

エラーは握りつぶさず、失敗理由を構造化ログへ残せるよう `reason` を持たせる。
"""

from __future__ import annotations


class FetchError(Exception):
    """記事取得の失敗を表す基底クラス。"""

    reason: str = "fetch_failed"


class UnsafeUrlError(FetchError):
    """安全性検証で拒否した URL。

    利用者へは詳細を返しすぎないよう、メッセージは内部ログ向けに留める。
    """

    reason = "unsafe_url"


class TooManyRedirectsError(FetchError):
    """リダイレクト回数が上限を超えた。"""

    reason = "too_many_redirects"


class ResponseTooLargeError(FetchError):
    """レスポンスが上限サイズを超えた。"""

    reason = "response_too_large"


class UnsupportedContentTypeError(FetchError):
    """HTML 以外の Content-Type を返された。"""

    reason = "unsupported_content_type"


class ExtractionError(FetchError):
    """本文を抽出できなかった。"""

    reason = "extraction_failed"
