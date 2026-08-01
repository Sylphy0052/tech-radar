"""URL 登録処理（fetch/analyze/embed）の失敗理由分類を検証する。"""

from __future__ import annotations

import pytest

from techradar.embedding.errors import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingModelLoadError,
)
from techradar.fetcher.errors import (
    ExtractionError,
    FetchError,
    TooManyRedirectsError,
    UnsafeUrlError,
)
from techradar.jobs.handlers.errors import (
    RegistrationErrorReason,
    classify_analysis_error,
    classify_embedding_error,
    classify_fetch_error,
)
from techradar.llm.errors import LLMError, LLMInvalidResponseError, LLMTimeoutError


class TestClassifyFetchError:
    def test_classifies_extraction_error_as_extraction_failed(self) -> None:
        # Arrange
        exc = ExtractionError("本文を抽出できませんでした")

        # Act
        reason = classify_fetch_error(exc)

        # Assert
        assert reason == RegistrationErrorReason.EXTRACTION_FAILED

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(TooManyRedirectsError("boom"), id="too-many-redirects"),
            pytest.param(UnsafeUrlError("boom"), id="unsafe-url"),
        ],
    )
    def test_classifies_other_fetch_errors_as_fetch_failed(self, exc: FetchError) -> None:
        # Act / Assert
        assert classify_fetch_error(exc) == RegistrationErrorReason.FETCH_FAILED

    def test_classifies_an_unknown_exception_as_unexpected_failure(self) -> None:
        """分類対象外の例外も理由を持たせる（登録を実行中のまま残さないため）。"""
        # Act / Assert
        assert (
            classify_fetch_error(RuntimeError("想定していない失敗"))
            == RegistrationErrorReason.UNEXPECTED_FAILURE
        )


class TestClassifyAnalysisError:
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(LLMTimeoutError("boom"), id="timeout"),
            pytest.param(LLMInvalidResponseError("boom"), id="invalid-response"),
        ],
    )
    def test_classifies_llm_errors_as_analysis_failed(self, exc: LLMError) -> None:
        # Act / Assert
        assert classify_analysis_error(exc) == RegistrationErrorReason.ANALYSIS_FAILED

    def test_classifies_an_unknown_exception_as_unexpected_failure(self) -> None:
        # Act / Assert
        assert (
            classify_analysis_error(RuntimeError("想定していない失敗"))
            == RegistrationErrorReason.UNEXPECTED_FAILURE
        )


class TestClassifyEmbeddingError:
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(EmbeddingModelLoadError("boom"), id="model-load"),
            pytest.param(EmbeddingDimensionMismatchError("boom"), id="dimension-mismatch"),
        ],
    )
    def test_classifies_embedding_errors_as_embedding_failed(self, exc: EmbeddingError) -> None:
        # Act / Assert
        assert classify_embedding_error(exc) == RegistrationErrorReason.EMBEDDING_FAILED

    def test_classifies_an_unknown_exception_as_unexpected_failure(self) -> None:
        # Act / Assert
        assert (
            classify_embedding_error(RuntimeError("想定していない失敗"))
            == RegistrationErrorReason.UNEXPECTED_FAILURE
        )
