"""リトライと構造化ログ記録を検証する。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db import Article, OperationLog
from techradar.llm import FakeLLMProvider, complete_json_with_retry
from techradar.llm.errors import (
    LLMInvocationError,
    LLMTimeoutError,
    LLMToolUseDetectedError,
)


class ArticleSummary(BaseModel):
    summary_ja: str
    topics: list[str]


VALID_RESPONSE = {"summary_ja": "要約", "topics": ["MCP"]}


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, llm_max_retries=3, llm_retry_backoff_seconds=0.01)


@pytest.fixture
def slept() -> list[float]:
    return []


def make_sleep(slept: list[float]):
    """待機時間を記録するだけの `sleep`。テストを遅くしない。"""

    def _sleep(seconds: float) -> None:
        slept.append(seconds)

    return _sleep


def run(provider, session, settings, slept, **overrides):
    """テストの定型呼び出し。"""
    return complete_json_with_retry(
        provider,
        instruction="要約してください",
        untrusted_content="本文",
        schema=ArticleSummary,
        operation="analyze_article",
        session=session,
        settings=settings,
        sleep=make_sleep(slept),
        **overrides,
    )


class TestRetryBehaviour:
    def test_returns_on_first_success(self, db_session: Session, settings, slept):
        # Arrange
        provider = FakeLLMProvider([VALID_RESPONSE])

        # Act
        completion = run(provider, db_session, settings, slept)

        # Assert
        assert completion.data == VALID_RESPONSE
        assert len(provider.calls) == 1
        assert slept == []

    def test_retries_until_success(self, db_session: Session, settings, slept):
        # Arrange — 2 回失敗してから成功する
        provider = FakeLLMProvider(
            [LLMTimeoutError("1回目"), LLMInvocationError("2回目"), VALID_RESPONSE]
        )

        # Act
        completion = run(provider, db_session, settings, slept)

        # Assert
        assert completion.data == VALID_RESPONSE
        assert len(provider.calls) == 3

    def test_uses_exponential_backoff(self, db_session: Session, settings, slept):
        # Arrange
        provider = FakeLLMProvider([LLMTimeoutError("1"), LLMTimeoutError("2"), VALID_RESPONSE])

        # Act
        run(provider, db_session, settings, slept)

        # Assert — base * 2^n
        assert slept == [0.01, 0.02]

    def test_gives_up_after_configured_attempts(self, db_session: Session, settings, slept):
        # Arrange — 常に失敗する
        provider = FakeLLMProvider([LLMTimeoutError("always")])

        # Act / Assert
        with pytest.raises(LLMTimeoutError):
            run(provider, db_session, settings, slept)

        # Assert — 初回 + リトライ 3 回
        assert len(provider.calls) == settings.llm_max_retries + 1

    def test_does_not_retry_tool_use_detection(self, db_session: Session, settings, slept):
        # Arrange — 隔離の失敗は繰り返しても解消しない
        provider = FakeLLMProvider([LLMToolUseDetectedError("tool used")])

        # Act / Assert
        with pytest.raises(LLMToolUseDetectedError):
            run(provider, db_session, settings, slept)

        # Assert
        assert len(provider.calls) == 1
        assert slept == []


class TestOperationLogging:
    def test_records_model_tokens_and_duration_on_success(
        self, db_session: Session, settings, slept
    ):
        # Arrange
        provider = FakeLLMProvider([VALID_RESPONSE])

        # Act
        run(provider, db_session, settings, slept)

        # Assert — §24 可観測性
        log = db_session.scalars(select(OperationLog)).one()
        assert log.operation == "analyze_article"
        assert log.status == "completed"
        assert log.model == "fake-model"
        assert log.input_tokens == 100
        assert log.output_tokens == 20
        assert log.duration_ms == 5

    def test_records_failure_reason_after_exhausting_retries(
        self, db_session: Session, settings, slept
    ):
        # Arrange
        provider = FakeLLMProvider([LLMTimeoutError("timed out")])

        # Act
        with pytest.raises(LLMTimeoutError):
            run(provider, db_session, settings, slept)

        # Assert
        log = db_session.scalars(select(OperationLog)).one()
        assert log.status == "failed"
        assert log.error_reason == "llm_timeout"
        assert log.details["attempts"] == settings.llm_max_retries + 1

    def test_records_tool_use_detection_as_non_retryable(
        self, db_session: Session, settings, slept
    ):
        # Arrange
        provider = FakeLLMProvider([LLMToolUseDetectedError("tool used")])

        # Act
        with pytest.raises(LLMToolUseDetectedError):
            run(provider, db_session, settings, slept)

        # Assert
        log = db_session.scalars(select(OperationLog)).one()
        assert log.error_reason == "llm_tool_use_detected"
        assert log.details["retryable"] is False

    def test_links_the_log_to_the_article(self, db_session: Session, settings, slept):
        # Arrange
        article = Article(
            canonical_url=f"https://example.com/{uuid.uuid4().hex[:8]}",
            original_url="https://example.com/a",
            title="T",
            source_domain="example.com",
        )
        db_session.add(article)
        db_session.flush()
        provider = FakeLLMProvider([VALID_RESPONSE])

        # Act
        run(provider, db_session, settings, slept, article_id=article.id)

        # Assert
        log = db_session.scalars(select(OperationLog)).one()
        assert log.article_id == article.id

    def test_works_without_a_session(self, settings, slept):
        # Arrange — DB を使わない経路でも呼び出せること
        provider = FakeLLMProvider([VALID_RESPONSE])

        # Act
        completion = run(provider, None, settings, slept)

        # Assert
        assert completion.data == VALID_RESPONSE


class TestProviderSubstitution:
    def test_fake_provider_satisfies_the_protocol(self):
        # Arrange — 将来 ASK API 等を追加できる抽象になっていること
        from techradar.llm.base import LLMProvider

        # Act / Assert
        assert isinstance(FakeLLMProvider([VALID_RESPONSE]), LLMProvider)

    def test_claude_cli_provider_satisfies_the_protocol(self):
        # Arrange
        from techradar.llm.base import LLMProvider
        from techradar.llm.claude_cli import ClaudeCliProvider

        # Act / Assert
        assert isinstance(ClaudeCliProvider(Settings(_env_file=None)), LLMProvider)

    def test_untrusted_content_reaches_the_provider_unchanged(
        self, db_session: Session, settings, slept
    ):
        # Arrange
        provider = FakeLLMProvider([VALID_RESPONSE])

        # Act
        run(provider, db_session, settings, slept)

        # Assert
        assert provider.calls[0]["untrusted_content"] == "本文"
