"""`analyze_article` ジョブハンドラを検証する結合テスト。

LLM は `FakeLLMProvider` に差し替えて呼ばない。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article, ArticleRegistration, Job
from techradar.fetcher.url import normalize_url
from techradar.jobs.handlers import analyze_article as analyze_article_handler
from techradar.jobs.handlers.analyze_article import process_analyze_article
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.registry import JobContext
from techradar.llm import FakeLLMProvider
from techradar.llm.errors import LLMTimeoutError

VALID_ANALYSIS = {
    "translated_title": "MCP サーバー実装ガイド",
    "summary_ja": "MCP を用いて LLM を外部ツールへ接続する手順を、実装例とともに解説する記事。",
    "domain": "Generative AI",
    "category": "Agentic Engineering",
    "topics": ["MCP", "Tool Use"],
    "technologies": ["Claude Code"],
    "content_type": "implementation",
    "difficulty": "intermediate",
    "technical_quality": 0.85,
}


def no_sleep(_seconds: float) -> None:
    """バックオフを待たない。テストを実時間で遅くしないため。"""


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_article(session: Session, *, body_hash: str | None = "hash-1") -> Article:
    article = Article(
        canonical_url=f"https://example.com/{uuid.uuid4().hex[:10]}",
        original_url="https://example.com/a",
        title="MCP Server Implementation Guide",
        body="Model Context Protocol is an open standard for connecting LLMs to tools.",
        body_hash=body_hash,
        source_domain="example.com",
    )
    session.add(article)
    session.flush()
    return article


def make_registration(session: Session, article: Article) -> ArticleRegistration:
    registration = ArticleRegistration(
        user_id=uuid.uuid4(),
        url="https://example.com/a",
        normalized_url=normalize_url("https://example.com/a"),
        status=JobStatus.FETCHING.value,
        article_id=article.id,
    )
    session.add(registration)
    session.flush()
    return registration


def make_context(
    registration: ArticleRegistration, article: Article, *, attempts: int = 0
) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.ANALYZE_ARTICLE,
        payload={"registration_id": str(registration.id), "article_id": str(article.id)},
        attempts=attempts,
    )


class TestProcessAnalyzeArticleSuccess:
    def test_sets_the_registration_status_to_analyzing_while_running(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert
        assert registration.status == JobStatus.ANALYZING.value

    def test_enqueues_an_embed_article_job_and_updates_the_registration_job_id(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.EMBED_ARTICLE.value)).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {
            "registration_id": str(registration.id),
            "article_id": str(article.id),
        }
        assert registration.job_id == jobs[0].id


class TestSkipsWhenAnalysisNotNeeded:
    def _mark_already_analyzed(self, article: Article, session: Session) -> None:
        article.summary_ja = "既存の要約"
        article.analyzed_body_hash = article.body_hash
        session.flush()

    def test_does_not_call_the_llm_when_the_body_is_unchanged(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        self._mark_already_analyzed(article, db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert — §24 コスト管理「同一記事の再解析を避ける」
        assert provider.calls == []

    def test_still_enqueues_embed_article_when_skipped(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        self._mark_already_analyzed(article, db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.EMBED_ARTICLE.value)).all()
        assert len(jobs) == 1


class TestProcessAnalyzeArticleFailure:
    def test_records_a_classified_reason_without_the_raw_exception_message(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        sensitive_detail = "boom with api-key=abcd1234"
        provider = FakeLLMProvider([LLMTimeoutError(sensitive_detail)])

        # Act
        with pytest.raises(LLMTimeoutError):
            process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert
        assert registration.error_reason == RegistrationErrorReason.ANALYSIS_FAILED.value
        assert sensitive_detail not in registration.error_reason

    def test_marks_the_registration_failed_once_the_retry_budget_is_exhausted(
        self, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article, attempts=0)
        provider = FakeLLMProvider([LLMTimeoutError("boom")])
        settings_one_attempt = Settings(_env_file=None, job_max_attempts=1)

        # Act
        with pytest.raises(LLMTimeoutError):
            process_analyze_article(
                db_session, context, settings_one_attempt, provider, sleep=no_sleep
            )

        # Assert
        assert registration.status == JobStatus.FAILED.value

    def test_keeps_the_registration_in_progress_while_retries_remain(
        self, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article, attempts=0)
        provider = FakeLLMProvider([LLMTimeoutError("boom")])
        settings_multi_attempt = Settings(_env_file=None, job_max_attempts=3)

        # Act
        with pytest.raises(LLMTimeoutError):
            process_analyze_article(
                db_session, context, settings_multi_attempt, provider, sleep=no_sleep
            )

        # Assert
        assert registration.status != JobStatus.FAILED.value


class TestProcessAnalyzeArticleMissingRow:
    def test_returns_without_raising_when_the_registration_is_missing(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        context = JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.ANALYZE_ARTICLE,
            payload={"registration_id": str(uuid.uuid4()), "article_id": str(uuid.uuid4())},
            attempts=0,
        )
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act / Assert — 例外を出さずに終了する
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)
        assert provider.calls == []

    def test_returns_without_raising_when_the_article_is_missing(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — registration はあるが参照先の記事が既に削除されている
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.ANALYZE_ARTICLE,
            payload={"registration_id": str(registration.id), "article_id": str(uuid.uuid4())},
            attempts=0,
        )
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act / Assert — 例外を出さずに終了する
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)
        assert provider.calls == []


class TestProcessAnalyzeArticleWithoutRegistration:
    """`registration_id` を持たない（巡回由来の）payload を検証する（Issue #9 T15）。"""

    def _make_context(self, article: Article) -> JobContext:
        return JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.ANALYZE_ARTICLE,
            payload={"article_id": str(article.id)},
            attempts=0,
        )

    def test_analyzes_the_article_without_raising_a_key_error(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        context = self._make_context(article)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act — registration_id が無くても KeyError にならない
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert
        assert article.summary_ja is not None

    def test_enqueues_an_embed_article_job_without_a_registration_id(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        context = self._make_context(article)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act
        process_analyze_article(db_session, context, settings, provider, sleep=no_sleep)

        # Assert
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.EMBED_ARTICLE.value)).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {"article_id": str(article.id)}

    def test_does_not_call_record_registration_failure_safely_on_error(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """失敗記録先の登録行が無いため、記録処理を呼ばず例外をそのまま送出する。"""
        # Arrange
        article = make_article(db_session)
        context = self._make_context(article)
        provider = FakeLLMProvider([LLMTimeoutError("boom")])

        was_called = False

        def _fail_if_called(*_args: object, **_kwargs: object) -> None:
            nonlocal was_called
            was_called = True

        monkeypatch.setattr(
            analyze_article_handler, "record_registration_failure_safely", _fail_if_called
        )

        # Act / Assert
        with pytest.raises(LLMTimeoutError):
            process_analyze_article(
                db_session, context, Settings(_env_file=None), provider, sleep=no_sleep
            )
        assert was_called is False
