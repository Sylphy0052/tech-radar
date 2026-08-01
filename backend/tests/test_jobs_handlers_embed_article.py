"""`embed_article` ジョブハンドラを検証する結合テスト。

実モデルは読み込まず `FakeEmbeddingProvider` を使う。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article, ArticleRegistration
from techradar.embedding import FakeEmbeddingProvider, embed_articles
from techradar.embedding.errors import EmbeddingModelLoadError
from techradar.fetcher.url import normalize_url
from techradar.jobs.handlers.embed_article import process_embed_article
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.registry import JobContext


class _FailingEmbeddingProvider:
    """常に失敗する `EmbeddingProvider`。実モデルを読み込まずに失敗経路を検証する。"""

    name = "failing"
    dimensions = 1024

    def __init__(self, message: str) -> None:
        self._message = message

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingModelLoadError(self._message)

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingModelLoadError(self._message)


class _UnexpectedlyFailingEmbeddingProvider:
    """`EmbeddingError` 以外で失敗する `EmbeddingProvider`（想定外の例外の再現用）。"""

    name = "unexpected"
    dimensions = 1024

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        message = "想定していない失敗"
        raise RuntimeError(message)

    def embed_query(self, text: str) -> list[float]:
        message = "想定していない失敗"
        raise RuntimeError(message)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_article(session: Session, *, body_hash: str | None = "hash-1") -> Article:
    article = Article(
        canonical_url=f"https://example.com/{uuid.uuid4().hex[:10]}",
        original_url="https://example.com/a",
        title="タイトル",
        body="本文" * 30,
        body_hash=body_hash,
        source_domain="example.com",
    )
    session.add(article)
    session.flush()
    return article


def make_registration(
    session: Session, article: Article, *, error_reason: str | None = None
) -> ArticleRegistration:
    registration = ArticleRegistration(
        user_id=uuid.uuid4(),
        url="https://example.com/a",
        normalized_url=normalize_url("https://example.com/a"),
        status=JobStatus.ANALYZING.value,
        article_id=article.id,
        error_reason=error_reason,
    )
    session.add(registration)
    session.flush()
    return registration


def make_context(
    registration: ArticleRegistration, article: Article, *, attempts: int = 0
) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.EMBED_ARTICLE,
        payload={"registration_id": str(registration.id), "article_id": str(article.id)},
        attempts=attempts,
    )


class TestProcessEmbedArticleSuccess:
    def test_generates_the_embedding(self, db_session: Session, settings: Settings) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = FakeEmbeddingProvider()

        # Act
        process_embed_article(db_session, context, settings, provider)

        # Assert
        assert article.embedding is not None

    def test_marks_the_registration_completed(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = FakeEmbeddingProvider()

        # Act
        process_embed_article(db_session, context, settings, provider)

        # Assert — URL 登録の状態遷移の終端（PROJECT_SPEC.md §6.2）
        assert registration.status == JobStatus.COMPLETED.value

    def test_clears_the_failure_reason_left_by_an_earlier_attempt(
        self, db_session: Session, settings: Settings
    ) -> None:
        """一時的な失敗のあと再試行が成功したら、古い失敗理由を残さない。

        `error_reason` はリトライ枠が残っている間も記録されるため、消さないと
        `status=completed` と失敗理由が同時に返る矛盾したレスポンスになる。
        """
        # Arrange
        article = make_article(db_session)
        registration = make_registration(
            db_session, article, error_reason=RegistrationErrorReason.EMBEDDING_FAILED.value
        )
        context = make_context(registration, article)
        provider = FakeEmbeddingProvider()

        # Act
        process_embed_article(db_session, context, settings, provider)

        # Assert
        assert registration.status == JobStatus.COMPLETED.value
        assert registration.error_reason is None


class TestSkipsWhenEmbeddingNotNeeded:
    def test_does_not_call_the_model_when_the_body_is_unchanged(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 事前に埋め込み済みにしておく
        article = make_article(db_session)
        provider = FakeEmbeddingProvider()
        embed_articles(db_session, provider, [article])
        provider.embedded_documents.clear()
        registration = make_registration(db_session, article)
        context = make_context(registration, article)

        # Act
        process_embed_article(db_session, context, settings, provider)

        # Assert — §24 コスト管理「既存記事の Embedding を再生成しない」
        assert provider.embedded_documents == []

    def test_still_marks_completed_when_skipped(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        provider = FakeEmbeddingProvider()
        embed_articles(db_session, provider, [article])
        registration = make_registration(db_session, article)
        context = make_context(registration, article)

        # Act
        process_embed_article(db_session, context, settings, provider)

        # Assert
        assert registration.status == JobStatus.COMPLETED.value


class TestProcessEmbedArticleFailure:
    def test_records_a_classified_reason_without_the_raw_exception_message(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        sensitive_detail = "boom loading model with token=abcd1234"
        provider = _FailingEmbeddingProvider(sensitive_detail)

        # Act
        with pytest.raises(EmbeddingModelLoadError):
            process_embed_article(db_session, context, settings, provider)

        # Assert
        assert registration.error_reason == RegistrationErrorReason.EMBEDDING_FAILED.value
        assert sensitive_detail not in registration.error_reason

    def test_marks_the_registration_failed_once_the_retry_budget_is_exhausted(
        self, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article, attempts=0)
        provider = _FailingEmbeddingProvider("boom")
        settings_one_attempt = Settings(_env_file=None, job_max_attempts=1)

        # Act
        with pytest.raises(EmbeddingModelLoadError):
            process_embed_article(db_session, context, settings_one_attempt, provider)

        # Assert
        assert registration.status == JobStatus.FAILED.value

    def test_keeps_the_registration_in_progress_while_retries_remain(
        self, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article, attempts=0)
        provider = _FailingEmbeddingProvider("boom")
        settings_multi_attempt = Settings(_env_file=None, job_max_attempts=3)

        # Act
        with pytest.raises(EmbeddingModelLoadError):
            process_embed_article(db_session, context, settings_multi_attempt, provider)

        # Assert
        assert registration.status != JobStatus.FAILED.value

    def test_records_a_generic_reason_for_an_unclassified_failure(
        self, db_session: Session, settings: Settings
    ) -> None:
        """分類対象外の例外でも、登録を実行中のまま取り残さない。

        分類済みの例外だけを記録する作りだと、想定外の例外で落ちたときに
        `status` が実行中・`error_reason` が空のまま固定され、UI からは
        永久に処理中に見える。
        """
        # Arrange
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = make_context(registration, article)
        provider = _UnexpectedlyFailingEmbeddingProvider()

        # Act
        with pytest.raises(RuntimeError):
            process_embed_article(db_session, context, settings, provider)

        # Assert
        assert registration.error_reason == RegistrationErrorReason.UNEXPECTED_FAILURE.value


class TestProcessEmbedArticleMissingRow:
    def test_returns_without_raising_when_the_registration_is_missing(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        context = JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.EMBED_ARTICLE,
            payload={"registration_id": str(uuid.uuid4()), "article_id": str(uuid.uuid4())},
            attempts=0,
        )
        provider = FakeEmbeddingProvider()

        # Act / Assert — 例外を出さずに終了する
        process_embed_article(db_session, context, settings, provider)
        assert provider.embedded_documents == []

    def test_returns_without_raising_when_the_article_is_missing(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — registration はあるが参照先の記事が既に削除されている
        article = make_article(db_session)
        registration = make_registration(db_session, article)
        context = JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.EMBED_ARTICLE,
            payload={"registration_id": str(registration.id), "article_id": str(uuid.uuid4())},
            attempts=0,
        )
        provider = FakeEmbeddingProvider()

        # Act / Assert — 例外を出さずに終了する
        process_embed_article(db_session, context, settings, provider)
        assert provider.embedded_documents == []
