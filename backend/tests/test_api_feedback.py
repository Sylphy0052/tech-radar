"""記事フィードバック API を検証する（`PROJECT_SPEC.md` §7, Issue #13）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, UserArticle
from techradar.db.enums import ArticleOrigin
from techradar.main import create_app

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def make_article(session: Session, *, title: str = "記事タイトル") -> Article:
    """フィードバック対象として使う記事を DB へ保存する。"""
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title=title,
        source_domain="example.com",
        fetched_at=NOW,
    )
    session.add(article)
    session.flush()
    return article


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def client(db_session: Session, settings: Settings) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。"""
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _feedback_count(db_session: Session, article_id: uuid.UUID) -> int:
    return (
        db_session.scalar(
            select(func.count())
            .select_from(ArticleFeedback)
            .where(ArticleFeedback.article_id == article_id)
        )
        or 0
    )


def _get_user_article(db_session: Session, article_id: uuid.UUID) -> UserArticle | None:
    return db_session.scalar(select(UserArticle).where(UserArticle.article_id == article_id))


class TestCreateArticleFeedback:
    def test_records_a_good_feedback(self, client: TestClient, db_session: Session) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        response = client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "good"
        assert body["reason"] is None

    def test_records_a_save_feedback(self, client: TestClient, db_session: Session) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        response = client.post(f"/api/articles/{article.id}/feedback", json={"action": "save"})

        # Assert
        assert response.status_code == 200
        assert response.json()["action"] == "save"

    def test_records_a_bad_feedback_without_a_reason(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        response = client.post(f"/api/articles/{article.id}/feedback", json={"action": "bad"})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "bad"
        assert body["reason"] is None

    def test_records_a_bad_feedback_with_a_reason(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        response = client.post(
            f"/api/articles/{article.id}/feedback",
            json={"action": "bad", "reason": "too_shallow"},
        )

        # Assert
        assert response.status_code == 200
        assert response.json()["reason"] == "too_shallow"

    def test_overwrites_the_existing_feedback_on_resubmission(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 同一記事への再送信で行が上書きされる（1 行のまま）。"""
        # Arrange
        article = make_article(db_session)
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Act
        response = client.post(
            f"/api/articles/{article.id}/feedback",
            json={"action": "bad", "reason": "not_interested"},
        )

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "bad"
        assert body["reason"] == "not_interested"
        assert _feedback_count(db_session, article.id) == 1

    def test_returns_404_for_an_unknown_article(self, client: TestClient) -> None:
        # Act
        response = client.post(f"/api/articles/{uuid.uuid4()}/feedback", json={"action": "good"})

        # Assert
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "invalid"},
            {"action": "bad", "reason": "invalid_reason"},
        ],
    )
    def test_rejects_an_unknown_action_or_reason(
        self, client: TestClient, payload: dict[str, str]
    ) -> None:
        """受入基準: action / reason が列挙外なら 422。"""
        # Act
        response = client.post(f"/api/articles/{uuid.uuid4()}/feedback", json=payload)

        # Assert
        assert response.status_code == 422

    def test_rejects_an_unknown_field(self, client: TestClient) -> None:
        # Act
        response = client.post(
            f"/api/articles/{uuid.uuid4()}/feedback",
            json={"action": "good", "unexpected": "x"},
        )

        # Assert
        assert response.status_code == 422

    def test_good_creates_a_user_article_with_the_expected_origin_and_weight(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Assert
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.GOOD.value
        assert user_article.interest_weight == pytest.approx(0.8)

    def test_save_creates_a_user_article_with_the_expected_origin_and_weight(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "save"})

        # Assert
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.SAVED.value
        assert user_article.interest_weight == pytest.approx(0.5)

    def test_good_does_not_downgrade_an_existing_heavier_origin(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: 既存の重い origin（手動登録 1.0）が good で下書きされない。"""
        # Arrange
        article = make_article(db_session)
        db_session.add(
            UserArticle(
                user_id=settings.default_user_id,
                article_id=article.id,
                origin=ArticleOrigin.MANUAL.value,
                interest_weight=1.0,
            )
        )
        db_session.flush()

        # Act
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Assert
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.MANUAL.value
        assert user_article.interest_weight == pytest.approx(1.0)

    def test_bad_removes_the_good_derived_user_article(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: bad で good 由来の user_articles が消える。"""
        # Arrange
        article = make_article(db_session)
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Act
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "bad"})

        # Assert
        assert _get_user_article(db_session, article.id) is None

    def test_bad_keeps_the_manual_derived_user_article(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: bad で manual 由来の user_articles が残る。"""
        # Arrange
        article = make_article(db_session)
        db_session.add(
            UserArticle(
                user_id=settings.default_user_id,
                article_id=article.id,
                origin=ArticleOrigin.MANUAL.value,
                interest_weight=1.0,
            )
        )
        db_session.flush()

        # Act
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "bad"})

        # Assert
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.MANUAL.value


class TestDeleteArticleFeedback:
    def test_deletes_the_feedback_and_the_good_derived_user_article(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: DELETE で feedback と good 由来 user_articles が消える。"""
        # Arrange
        article = make_article(db_session)
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Act
        response = client.delete(f"/api/articles/{article.id}/feedback")

        # Assert
        assert response.status_code == 204
        assert db_session.get(ArticleFeedback, (settings.default_user_id, article.id)) is None
        assert _get_user_article(db_session, article.id) is None

    def test_keeps_the_manual_derived_user_article(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: DELETE で manual 由来が残る。"""
        # Arrange
        article = make_article(db_session)
        db_session.add(
            UserArticle(
                user_id=settings.default_user_id,
                article_id=article.id,
                origin=ArticleOrigin.MANUAL.value,
                interest_weight=1.0,
            )
        )
        db_session.flush()
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Act
        response = client.delete(f"/api/articles/{article.id}/feedback")

        # Assert
        assert response.status_code == 204
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.MANUAL.value

    def test_returns_404_when_no_feedback_exists(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: DELETE 対象が無い場合 404。"""
        # Arrange
        article = make_article(db_session)

        # Act
        response = client.delete(f"/api/articles/{article.id}/feedback")

        # Assert
        assert response.status_code == 404
