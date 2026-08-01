"""URL 登録処理（fetch_article / analyze_article / embed_article）の失敗理由分類。

`article_registrations.error_reason` は登録状態確認 API から直接返す値のため、
例外メッセージそのもの（アクセス先 URL や API キーが混ざりうる）を入れない。
ここで例外の種類を、ユーザーに見せてよい粒度の値へ写像する。
"""

from __future__ import annotations

from enum import StrEnum

from techradar.embedding.errors import EmbeddingError
from techradar.fetcher.errors import ExtractionError, FetchError
from techradar.llm.errors import LLMError


class RegistrationErrorReason(StrEnum):
    """URL 登録の失敗理由（ユーザーに見せてよい粒度）。"""

    FETCH_FAILED = "fetch_failed"
    EXTRACTION_FAILED = "extraction_failed"
    ANALYSIS_FAILED = "analysis_failed"
    EMBEDDING_FAILED = "embedding_failed"


def classify_fetch_error(exc: FetchError) -> RegistrationErrorReason:
    """記事取得系の例外を、ユーザーに見せてよい理由へ分類する。

    `ExtractionError` は「アクセス自体はできたが本文を取り出せなかった」ことを
    表す。ネットワーク到達性や SSRF 拒否など他の取得失敗とは利用者から見た
    次の一手（別 URL を試す等）が異なるため、別の理由として区別する。
    """
    if isinstance(exc, ExtractionError):
        return RegistrationErrorReason.EXTRACTION_FAILED
    return RegistrationErrorReason.FETCH_FAILED


def classify_analysis_error(exc: LLMError) -> RegistrationErrorReason:
    """解析 (LLM) 系の例外を分類する。

    現時点では LLM 呼び出し失敗の内訳（タイムアウト・不正応答等）を
    利用者へ出し分ける要件が無いため、1 種類の理由へ集約する。
    """
    del exc
    return RegistrationErrorReason.ANALYSIS_FAILED


def classify_embedding_error(exc: EmbeddingError) -> RegistrationErrorReason:
    """Embedding 系の例外を分類する。"""
    del exc
    return RegistrationErrorReason.EMBEDDING_FAILED
