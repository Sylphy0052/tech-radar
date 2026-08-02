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
from techradar.recommendation.config import get_scoring_config

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


def make_article(
    session: Session,
    *,
    topics: list[str] | None = None,
    technologies: list[str] | None = None,
    domain: str | None = None,
    content_type: str | None = None,
    difficulty: str | None = None,
    is_primary_source: bool = False,
) -> Article:
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title="タイトル",
        source_domain="example.com",
        topics=topics or [],
        technologies=technologies or [],
        domain=domain,
        content_type=content_type,
        difficulty=difficulty,
        is_primary_source=is_primary_source,
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


class TestInterestSummary:
    def test_returns_all_empty_when_no_data(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["genres"] == []
        assert body["technologies"] == []
        assert body["content_types"] == []
        assert body["difficulties"] == []
        assert body["suppressed_topics"] == []
        assert body["feedback_ratio"] == {"good_count": 0, "bad_count": 0, "save_count": 0}
        assert body["primary_source_ratio"] == {"primary_count": 0, "secondary_count": 0}

    def test_counts_feedback_ratio_by_action(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        for action in ("good", "good", "save", "bad"):
            article = make_article(db_session)
            add_feedback(db_session, user_id, article, action, created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert response.json()["feedback_ratio"] == {
            "good_count": 2,
            "bad_count": 1,
            "save_count": 1,
        }

    def test_content_distribution_counts_only_positive_actions(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — bad を付けた記事の content_type は数えない
        user_id = settings.default_user_id
        good_article = make_article(db_session, content_type="news")
        add_feedback(db_session, user_id, good_article, "good", created_at=NOW)
        bad_article = make_article(db_session, content_type="news")
        add_feedback(db_session, user_id, bad_article, "bad", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        content_types = {
            item["content_type"]: item["count"] for item in response.json()["content_types"]
        }
        assert content_types == {"news": 1}

    def test_unclassified_fields_appear_as_none_bucket(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — domain / content_type / difficulty すべて未設定の記事
        user_id = settings.default_user_id
        article = make_article(db_session)
        add_feedback(db_session, user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        body = response.json()
        assert body["genres"] == [{"domain": None, "positive_count": 1, "negative_count": 0}]
        assert body["content_types"] == [{"content_type": None, "count": 1}]
        assert body["difficulties"] == [{"difficulty": None, "count": 1}]

    def test_genres_report_positive_and_negative_counts_per_domain(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — ジャンル別関心度は good/save だけでなく bad の件数も返す
        user_id = settings.default_user_id
        good_article = make_article(db_session, domain="ai")
        add_feedback(db_session, user_id, good_article, "good", created_at=NOW)
        bad_article = make_article(db_session, domain="ai")
        add_feedback(db_session, user_id, bad_article, "bad", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        genre = next(item for item in response.json()["genres"] if item["domain"] == "ai")
        assert genre == {"domain": "ai", "positive_count": 1, "negative_count": 1}

    def test_genres_order_by_positive_count_desc_then_domain_asc(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        for domain, good_count in (("zeta", 1), ("alpha", 2), ("beta", 2)):
            for _ in range(good_count):
                article = make_article(db_session, domain=domain)
                add_feedback(db_session, user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        domains = [item["domain"] for item in response.json()["genres"]]
        assert domains == ["alpha", "beta", "zeta"]

    def test_technologies_are_expanded_from_jsonb_array_and_ordered_by_count_desc(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        article_a = make_article(db_session, technologies=["python", "rust"])
        add_feedback(db_session, user_id, article_a, "good", created_at=NOW)
        article_b = make_article(db_session, technologies=["python"])
        add_feedback(db_session, user_id, article_b, "save", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert response.json()["technologies"] == [
            {"technology": "python", "count": 2},
            {"technology": "rust", "count": 1},
        ]

    def test_primary_source_ratio_counts_only_positive_actions(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        primary_article = make_article(db_session, is_primary_source=True)
        add_feedback(db_session, user_id, primary_article, "good", created_at=NOW)
        secondary_article = make_article(db_session, is_primary_source=False)
        add_feedback(db_session, user_id, secondary_article, "save", created_at=NOW)
        excluded_article = make_article(db_session, is_primary_source=True)
        add_feedback(db_session, user_id, excluded_article, "bad", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert response.json()["primary_source_ratio"] == {"primary_count": 1, "secondary_count": 1}

    def test_suppressed_topics_only_include_positive_negative_weight(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = settings.default_user_id
        add_topic_preference(db_session, user_id, "suppressed", negative=0.4, effective=0.2)
        add_topic_preference(db_session, user_id, "not-suppressed", negative=0.0, effective=0.8)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        topics = [item["topic"] for item in response.json()["suppressed_topics"]]
        assert topics == ["suppressed"]

    def test_suppressed_topics_do_not_mix_in_another_users_rows(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        add_topic_preference(
            db_session, settings.default_user_id, "mine", negative=0.5, effective=0.3
        )
        add_topic_preference(db_session, uuid.uuid4(), "other", negative=0.9, effective=0.1)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        topics = [item["topic"] for item in response.json()["suppressed_topics"]]
        assert topics == ["mine"]

    def test_does_not_mix_in_another_users_feedback(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        other_user_id = uuid.uuid4()
        article = make_article(db_session, domain="ai")
        add_feedback(db_session, other_user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        body = response.json()
        assert body["feedback_ratio"] == {"good_count": 0, "bad_count": 0, "save_count": 0}
        assert body["genres"] == []

    def test_max_genres_caps_the_response(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — config の max_genres（既定20）を超えるジャンル数を用意する
        user_id = settings.default_user_id
        max_genres = get_scoring_config().interest_summary.max_genres
        for index in range(max_genres + 5):
            article = make_article(db_session, domain=f"domain-{index:03d}")
            add_feedback(db_session, user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert len(response.json()["genres"]) == max_genres

    def test_max_technologies_caps_the_response(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — config の max_technologies（既定20）を超える技術タグ数を用意する
        user_id = settings.default_user_id
        max_technologies = get_scoring_config().interest_summary.max_technologies
        for index in range(max_technologies + 5):
            article = make_article(db_session, technologies=[f"tech-{index:03d}"])
            add_feedback(db_session, user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert len(response.json()["technologies"]) == max_technologies

    def test_max_suppressed_topics_caps_the_response(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — config の max_suppressed_topics（既定20）を超える抑制トピック数を用意する
        user_id = settings.default_user_id
        max_suppressed_topics = get_scoring_config().interest_summary.max_suppressed_topics
        for index in range(max_suppressed_topics + 5):
            add_topic_preference(
                db_session, user_id, f"topic-{index:03d}", negative=0.5, effective=0.2
            )

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert len(response.json()["suppressed_topics"]) == max_suppressed_topics

    def test_max_content_types_caps_the_response(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — DB 列は制約の無い text のため、列挙外の content_type も入りうる
        user_id = settings.default_user_id
        max_content_types = get_scoring_config().interest_summary.max_content_types
        for index in range(max_content_types + 5):
            article = make_article(db_session, content_type=f"content-type-{index:03d}")
            add_feedback(db_session, user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert len(response.json()["content_types"]) == max_content_types

    def test_max_difficulties_caps_the_response(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — difficulty も content_type と同じく DB 側に制約が無い
        user_id = settings.default_user_id
        max_difficulties = get_scoring_config().interest_summary.max_difficulties
        for index in range(max_difficulties + 5):
            article = make_article(db_session, difficulty=f"difficulty-{index:03d}")
            add_feedback(db_session, user_id, article, "good", created_at=NOW)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        assert len(response.json()["difficulties"]) == max_difficulties

    def test_suppressed_topics_are_ordered_by_negative_weight_desc_then_topic(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 抑制度が同値のトピックを含めて並び順のタイブレークまで見る
        user_id = settings.default_user_id
        add_topic_preference(db_session, user_id, "zeta", negative=0.2, effective=0.1)
        add_topic_preference(db_session, user_id, "alpha", negative=0.2, effective=0.1)
        add_topic_preference(db_session, user_id, "beta", negative=0.6, effective=0.1)

        # Act
        response = client.get("/api/interests/summary")

        # Assert
        topics = [item["topic"] for item in response.json()["suppressed_topics"]]
        assert topics == ["beta", "alpha", "zeta"]
