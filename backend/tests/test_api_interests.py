"""関心プロファイル閲覧 API を検証する（`PROJECT_SPEC.md` §7, §8, Issue #15 段階 3）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db.models import (
    Article,
    ArticleFeedback,
    UserArticle,
    UserInterestCluster,
    UserTopicPreference,
)
from techradar.main import create_app

NOW = datetime(2026, 8, 1, tzinfo=UTC)
EMBEDDING_DIM = 1024


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def client(db_session: Session, settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def add_topic_preference(
    session: Session,
    user_id: uuid.UUID,
    topic: str,
    *,
    positive: float = 0.8,
    negative: float = 0.0,
    effective: float | None = None,
    updated_at: datetime | None = None,
) -> UserTopicPreference:
    row = UserTopicPreference(
        user_id=user_id,
        topic=topic,
        positive_weight=positive,
        negative_weight=negative,
        effective_weight=effective if effective is not None else positive,
        updated_at=updated_at or NOW,
    )
    session.add(row)
    session.flush()
    return row


def add_cluster(
    session: Session,
    user_id: uuid.UUID,
    label: str,
    *,
    weight: float,
    topics: list[str] | None = None,
) -> UserInterestCluster:
    row = UserInterestCluster(
        user_id=user_id,
        label=label,
        weight=weight,
        topics=topics or [],
        centroid_embedding=[0.0] * EMBEDDING_DIM,
        updated_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def make_article(session: Session, *, topics: list[str] | None = None) -> Article:
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title="タイトル",
        source_domain="example.com",
        topics=topics or [],
        fetched_at=NOW,
    )
    session.add(article)
    session.flush()
    return article


def add_feedback(
    session: Session,
    user_id: uuid.UUID,
    article: Article,
    action: str,
    *,
    created_at: datetime,
) -> ArticleFeedback:
    row = ArticleFeedback(
        user_id=user_id, article_id=article.id, action=action, created_at=created_at
    )
    session.add(row)
    session.flush()
    return row


def add_user_article(
    session: Session, user_id: uuid.UUID, article: Article, *, created_at: datetime
) -> UserArticle:
    row = UserArticle(
        user_id=user_id,
        article_id=article.id,
        origin="good",
        interest_weight=0.8,
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    return row


class TestListInterestTopics:
    def test_orders_by_effective_weight_desc_then_topic_asc(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        add_topic_preference(db_session, user_id, "zeta", positive=0.9, effective=0.9)
        add_topic_preference(db_session, user_id, "alpha", positive=0.9, effective=0.9)
        add_topic_preference(db_session, user_id, "beta", positive=0.3, effective=0.3)

        # Act
        response = client.get("/api/interests")

        # Assert
        assert response.status_code == 200
        topics = [item["topic"] for item in response.json()["items"]]
        assert topics == ["alpha", "zeta", "beta"]

    def test_response_includes_the_weight_fields(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        add_topic_preference(
            db_session, settings.default_user_id, "llm", positive=0.8, negative=0.2, effective=0.7
        )

        # Act
        response = client.get("/api/interests")

        # Assert
        item = response.json()["items"][0]
        assert item["topic"] == "llm"
        assert item["positive_weight"] == pytest.approx(0.8)
        assert item["negative_weight"] == pytest.approx(0.2)
        assert item["effective_weight"] == pytest.approx(0.7)
        assert "updated_at" in item

    def test_does_not_mix_in_another_users_topics(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        add_topic_preference(db_session, settings.default_user_id, "mine", effective=0.5)
        add_topic_preference(db_session, uuid.uuid4(), "other", effective=0.9)

        # Act
        response = client.get("/api/interests")

        # Assert
        topics = [item["topic"] for item in response.json()["items"]]
        assert topics == ["mine"]

    def test_limit_caps_the_page_size(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        for index in range(3):
            add_topic_preference(
                db_session, settings.default_user_id, f"topic-{index}", effective=1.0 - index * 0.1
            )

        # Act
        response = client.get("/api/interests", params={"limit": 2})

        # Assert
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2

    def test_rejects_a_limit_above_the_configured_maximum(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/interests", params={"limit": 10_000})
        # Assert
        assert response.status_code == 422

    def test_rejects_a_non_positive_limit(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/interests", params={"limit": 0})
        # Assert
        assert response.status_code == 422


class TestListInterestClusters:
    def test_orders_by_weight_desc_then_label_asc(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        add_cluster(db_session, user_id, "zeta", weight=0.5)
        add_cluster(db_session, user_id, "alpha", weight=0.5)
        add_cluster(db_session, user_id, "beta", weight=0.2)

        # Act
        response = client.get("/api/interests/clusters")

        # Assert
        assert response.status_code == 200
        labels = [item["label"] for item in response.json()["items"]]
        assert labels == ["alpha", "zeta", "beta"]

    def test_does_not_return_the_centroid_embedding(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        add_cluster(db_session, settings.default_user_id, "AI", weight=1.0, topics=["MCP"])

        # Act
        response = client.get("/api/interests/clusters")

        # Assert
        item = response.json()["items"][0]
        assert item["label"] == "AI"
        assert item["weight"] == pytest.approx(1.0)
        assert item["topics"] == ["MCP"]
        assert "updated_at" in item
        assert "centroid_embedding" not in item
        assert "centroid" not in item

    def test_does_not_mix_in_another_users_clusters(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        add_cluster(db_session, settings.default_user_id, "mine", weight=1.0)
        add_cluster(db_session, uuid.uuid4(), "other", weight=1.0)

        # Act
        response = client.get("/api/interests/clusters")

        # Assert
        labels = [item["label"] for item in response.json()["items"]]
        assert labels == ["mine"]


class TestInterestTimeline:
    def test_buckets_feedback_by_week_and_aggregates_topics(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 同じ週に Good x2, Bad x1 を同一トピックへ
        user_id = settings.default_user_id
        week_start = datetime(2026, 7, 27, tzinfo=UTC)  # 月曜日 00:00 UTC
        article_a = make_article(db_session, topics=["llm"])
        add_feedback(
            db_session, user_id, article_a, "good", created_at=week_start + timedelta(hours=1)
        )
        article_b = make_article(db_session, topics=["llm"])
        add_feedback(
            db_session, user_id, article_b, "save", created_at=week_start + timedelta(hours=2)
        )
        article_c = make_article(db_session, topics=["llm"])
        add_feedback(
            db_session, user_id, article_c, "bad", created_at=week_start + timedelta(hours=3)
        )

        # Act
        response = client.get("/api/interests/timeline", params={"weeks": 4})

        # Assert
        assert response.status_code == 200
        buckets = response.json()["buckets"]
        matching = [bucket for bucket in buckets if bucket["week_start"].startswith("2026-07-27")]
        assert len(matching) == 1
        topic_stats = {topic["topic"]: topic for topic in matching[0]["topics"]}
        assert topic_stats["llm"]["positive_count"] == 2
        assert topic_stats["llm"]["negative_count"] == 1

    def test_separates_different_weeks_into_different_buckets(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        week_1 = datetime(2026, 7, 6, tzinfo=UTC)
        week_2 = datetime(2026, 7, 13, tzinfo=UTC)
        article_1 = make_article(db_session, topics=["rag"])
        add_feedback(db_session, user_id, article_1, "good", created_at=week_1 + timedelta(hours=1))
        article_2 = make_article(db_session, topics=["rag"])
        add_feedback(db_session, user_id, article_2, "good", created_at=week_2 + timedelta(hours=1))

        # Act
        response = client.get("/api/interests/timeline", params={"weeks": 12})

        # Assert
        weeks = {
            bucket["week_start"][:10]
            for bucket in response.json()["buckets"]
            if any(topic["topic"] == "rag" for topic in bucket["topics"])
        }
        assert weeks == {"2026-07-06", "2026-07-13"}

    def test_includes_the_interest_article_count_from_user_articles(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        week_start = datetime(2026, 7, 27, tzinfo=UTC)
        article = make_article(db_session)
        add_user_article(db_session, user_id, article, created_at=week_start + timedelta(hours=1))

        # Act
        response = client.get("/api/interests/timeline", params={"weeks": 4})

        # Assert
        buckets = response.json()["buckets"]
        matching = [bucket for bucket in buckets if bucket["week_start"].startswith("2026-07-27")]
        assert len(matching) == 1
        assert matching[0]["interest_article_count"] == 1

    def test_rejects_weeks_above_the_configured_maximum(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/interests/timeline", params={"weeks": 10_000})
        # Assert
        assert response.status_code == 422

    def test_rejects_a_non_positive_weeks(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/interests/timeline", params={"weeks": 0})
        # Assert
        assert response.status_code == 422

    def test_does_not_mix_in_another_users_feedback(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        other_user_id = uuid.uuid4()
        week_start = datetime(2026, 7, 27, tzinfo=UTC)
        article = make_article(db_session, topics=["llm"])
        add_feedback(
            db_session, other_user_id, article, "good", created_at=week_start + timedelta(hours=1)
        )

        # Act
        response = client.get("/api/interests/timeline", params={"weeks": 4})

        # Assert
        buckets = response.json()["buckets"]
        assert all(len(bucket["topics"]) == 0 for bucket in buckets)
