"""`fetch_article` ジョブハンドラを検証する結合テスト。

実 HTTP へは出ず、`test_fetcher_service.py` と同じ手法で `httpx.Client` を
差し替える。DNS 解決も `test_fetcher_ssrf.py` の fake resolver で固定する。
"""

from __future__ import annotations

import socket
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import ArticleOrigin, JobStatus, JobType
from techradar.db.models import Article, ArticleRegistration, Job, UserArticle
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.errors import ExtractionError, TooManyRedirectsError
from techradar.fetcher.url import normalize_url
from techradar.jobs.handlers import fetch_article as fetch_article_handler
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.handlers.fetch_article import process_fetch_article
from techradar.jobs.registry import JobContext
from tests.test_fetcher_extract import article_html
from tests.test_fetcher_http import mock_client
from tests.test_fetcher_ssrf import fake_getaddrinfo


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def fetch_transport(monkeypatch: pytest.MonkeyPatch):
    """常に固定の記事 HTML を返す `httpx.Client` の差し替え。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=article_html(),
        )

    monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))


def make_registration(
    session: Session,
    url: str = "https://example.com/posts/1",
    *,
    user_id: uuid.UUID | None = None,
) -> ArticleRegistration:
    registration = ArticleRegistration(
        user_id=user_id or uuid.uuid4(),
        url=url,
        normalized_url=normalize_url(url),
        status=JobStatus.PENDING.value,
    )
    session.add(registration)
    session.flush()
    return registration


def make_context(registration: ArticleRegistration, *, attempts: int = 0) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.FETCH_ARTICLE,
        payload={"registration_id": str(registration.id), "url": registration.url},
        attempts=attempts,
    )


class TestProcessFetchArticleSuccess:
    def test_sets_the_article_id_on_success(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration)

        # Act
        process_fetch_article(db_session, context, settings)

        # Assert
        assert registration.article_id is not None

    def test_creates_a_user_article_with_manual_origin_and_full_weight(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration)

        # Act
        process_fetch_article(db_session, context, settings)

        # Assert — 手動 URL 登録の重み（PROJECT_SPEC.md §7.1）
        user_article = db_session.scalars(
            select(UserArticle).where(UserArticle.article_id == registration.article_id)
        ).one()
        assert user_article.user_id == registration.user_id
        assert user_article.origin == ArticleOrigin.MANUAL.value
        assert user_article.interest_weight == pytest.approx(1.0)

    def test_does_not_duplicate_the_user_article_when_two_registrations_resolve_to_the_same_article(
        self, db_session: Session, public_dns, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 同じ記事を再登録しても user_articles が重複しない。"""
        # Arrange — 異なる URL でも canonical が同じ記事に解決する状況を作る
        canonical_url = "https://example.com/posts/1"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=article_html(extra_head=f'<link rel="canonical" href="{canonical_url}">'),
            )

        monkeypatch.setattr(fetcher_http.httpx, "Client", mock_client(handler))
        user_id = uuid.uuid4()
        first = make_registration(db_session, "https://example.com/posts/1", user_id=user_id)
        second = make_registration(db_session, "https://example.com/alt-posts/1", user_id=user_id)

        # Act
        process_fetch_article(db_session, make_context(first), settings)
        process_fetch_article(db_session, make_context(second), settings)

        # Assert
        assert first.article_id == second.article_id
        user_articles = db_session.scalars(
            select(UserArticle).where(UserArticle.article_id == first.article_id)
        ).all()
        assert len(user_articles) == 1


class TestProcessFetchArticleEnqueuesAnalysis:
    def test_enqueues_an_analyze_article_job_and_updates_the_registration_job_id(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration)

        # Act
        process_fetch_article(db_session, context, settings)

        # Assert
        jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.ANALYZE_ARTICLE.value)
        ).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {
            "registration_id": str(registration.id),
            "article_id": str(registration.article_id),
        }
        assert registration.job_id == jobs[0].id

    def test_leaves_the_registration_status_as_fetching_after_a_successful_fetch(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        """fetch 完了時点では analyzing にせず、次のハンドラが自分の開始時に更新する
        （実装コメント参照。段階の責務を分けるための設計判断）。
        """
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration)

        # Act
        process_fetch_article(db_session, context, settings)

        # Assert
        assert registration.status == JobStatus.FETCHING.value


class TestProcessFetchArticleFailure:
    def test_records_a_classified_reason_without_the_raw_exception_message(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration)
        sensitive_detail = "boom while talking to https://internal.example/?token=abcd1234"

        def _raise_extraction_error(*_args: object, **_kwargs: object) -> None:
            raise ExtractionError(sensitive_detail)

        monkeypatch.setattr(fetch_article_handler, "ingest_article", _raise_extraction_error)

        # Act
        with pytest.raises(ExtractionError):
            process_fetch_article(db_session, context, settings)

        # Assert
        assert registration.error_reason == RegistrationErrorReason.EXTRACTION_FAILED.value
        assert sensitive_detail not in registration.error_reason

    def test_marks_the_registration_failed_once_the_retry_budget_is_exhausted(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration, attempts=0)
        settings_one_attempt = Settings(_env_file=None, job_max_attempts=1)

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise TooManyRedirectsError("boom")

        monkeypatch.setattr(fetch_article_handler, "ingest_article", _raise)

        # Act
        with pytest.raises(TooManyRedirectsError):
            process_fetch_article(db_session, context, settings_one_attempt)

        # Assert
        assert registration.status == JobStatus.FAILED.value
        assert registration.error_reason == RegistrationErrorReason.FETCH_FAILED.value

    def test_keeps_the_registration_in_progress_while_retries_remain(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: リトライ途中は failed にせず、UI 側が区別できるようにする。"""
        # Arrange
        registration = make_registration(db_session)
        context = make_context(registration, attempts=0)
        settings_multi_attempt = Settings(_env_file=None, job_max_attempts=3)

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise TooManyRedirectsError("boom")

        monkeypatch.setattr(fetch_article_handler, "ingest_article", _raise)

        # Act
        with pytest.raises(TooManyRedirectsError):
            process_fetch_article(db_session, context, settings_multi_attempt)

        # Assert
        assert registration.status != JobStatus.FAILED.value
        assert registration.error_reason == RegistrationErrorReason.FETCH_FAILED.value


class TestProcessFetchArticleMissingRegistration:
    def test_returns_without_raising_when_the_registration_is_missing(
        self, db_session: Session, settings: Settings
    ) -> None:
        """登録行が既に削除されている場合、リトライしても解決しないため打ち切る。"""
        # Arrange
        context = JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.FETCH_ARTICLE,
            payload={"registration_id": str(uuid.uuid4()), "url": "https://example.com/gone"},
            attempts=0,
        )

        # Act / Assert — 例外を出さずに終了する
        process_fetch_article(db_session, context, settings)


class TestProcessFetchArticleWithoutRegistration:
    """`registration_id` を持たない（巡回由来の）payload を検証する（Issue #9 T15）。"""

    def _make_context(self, url: str = "https://example.com/posts/1") -> JobContext:
        return JobContext(
            job_id=uuid.uuid4(),
            job_type=JobType.FETCH_ARTICLE,
            payload={"url": url, "origin": "crawl_sources"},
            attempts=0,
        )

    def test_saves_the_article_without_raising_a_key_error(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        # Arrange
        context = self._make_context()

        # Act — registration_id が無くても KeyError にならない
        process_fetch_article(db_session, context, settings)

        # Assert
        article = db_session.scalars(
            select(Article).where(Article.canonical_url == normalize_url(context.payload["url"]))
        ).one()
        assert article is not None

    def test_does_not_add_a_user_article(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        """受入基準: 巡回由来の候補は手動登録と同じ関心の重みを与えないため
        `user_articles` へは追加しない。
        """
        # Arrange
        context = self._make_context()

        # Act
        process_fetch_article(db_session, context, settings)

        # Assert
        article = db_session.scalars(
            select(Article).where(Article.canonical_url == normalize_url(context.payload["url"]))
        ).one()
        user_articles = db_session.scalars(
            select(UserArticle).where(UserArticle.article_id == article.id)
        ).all()
        assert user_articles == []

    def test_enqueues_an_analyze_article_job_without_a_registration_id(
        self, db_session: Session, public_dns, fetch_transport, settings: Settings
    ) -> None:
        # Arrange
        context = self._make_context()

        # Act
        process_fetch_article(db_session, context, settings)

        # Assert
        article = db_session.scalars(
            select(Article).where(Article.canonical_url == normalize_url(context.payload["url"]))
        ).one()
        jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.ANALYZE_ARTICLE.value)
        ).all()
        assert len(jobs) == 1
        assert jobs[0].payload == {"article_id": str(article.id)}

    def test_does_not_call_record_registration_failure_safely_on_error(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """失敗記録先の登録行が無いため、記録処理を呼ばず例外をそのまま送出する。"""
        # Arrange
        context = self._make_context()

        def _raise_extraction_error(*_args: object, **_kwargs: object) -> None:
            raise ExtractionError("boom")

        monkeypatch.setattr(fetch_article_handler, "ingest_article", _raise_extraction_error)

        was_called = False

        def _fail_if_called(*_args: object, **_kwargs: object) -> None:
            nonlocal was_called
            was_called = True

        monkeypatch.setattr(
            fetch_article_handler, "record_registration_failure_safely", _fail_if_called
        )

        # Act / Assert
        with pytest.raises(ExtractionError):
            process_fetch_article(db_session, context, settings)
        assert was_called is False
