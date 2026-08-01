"""記事起点推薦と Discover フィードの API を検証する（`PROJECT_SPEC.md` §6.1, §13, §20）。"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.api.deps import get_now, get_session
from techradar.api.recommendations import _MAX_CURSOR_RANK_DIGITS, CURSOR_MAX_LENGTH
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, Recommendation, RecommendationRun
from techradar.db.enums import FeedbackAction, RecommendationMode
from techradar.main import create_app
from techradar.recommendation.config import get_scoring_config
from techradar.recommendation.service import find_latest_run

NOW = datetime(2026, 8, 1, tzinfo=UTC)
EMBEDDING_DIM = 1024


def make_embedding(active_index: int = 0) -> list[float]:
    """1 箇所だけ 1.0 を立てたベクトルを返す（`test_recommendation_service.py` と同じ考え方）。

    同じ `active_index` 同士はコサイン類似度 1.0、異なる場合は 0.0 になるため、
    ランキング順序をテストで制御しやすい。
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[active_index] = 1.0
    return vector


def make_article(
    session: Session,
    *,
    title: str = "記事タイトル",
    body: str | None = None,
    source_domain: str = "example.com",
    topics: Sequence[str] = (),
    technical_quality: float = 0.5,
    published_at: datetime | None = NOW,
    embedding: list[float] | None = None,
) -> Article:
    """推薦候補として使う記事を DB へ保存する。"""
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title=title,
        body=body,
        source_domain=source_domain,
        topics=list(topics),
        technical_quality=technical_quality,
        published_at=published_at,
        fetched_at=NOW,
        embedding=embedding,
    )
    session.add(article)
    session.flush()
    return article


def add_bad_feedback(session: Session, user_id: uuid.UUID, article: Article) -> None:
    """指定記事を Bad 済みにする。"""
    session.add(
        ArticleFeedback(
            user_id=user_id,
            article_id=article.id,
            action=FeedbackAction.BAD.value,
            created_at=NOW,
        )
    )
    session.flush()


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


class TestCreateArticleRecommendations:
    def test_ranks_related_articles_ascending_and_excludes_the_source_article(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 起点記事自身を含まず、rank 昇順で返す
        source_article = make_article(
            db_session, title="起点記事", embedding=make_embedding(0), topics=["llm"]
        )
        related_article = make_article(
            db_session, title="近い記事", embedding=make_embedding(0), topics=["llm"]
        )
        unrelated_article = make_article(
            db_session, title="遠い記事", embedding=make_embedding(5), topics=["css"]
        )

        # Act
        response = client.post(f"/api/articles/{source_article.id}/recommendations")

        # Assert
        assert response.status_code == 200
        body = response.json()
        item_ids = [item["article_id"] for item in body["items"]]
        assert str(source_article.id) not in item_ids
        assert str(related_article.id) in item_ids
        assert str(unrelated_article.id) in item_ids
        assert [item["rank"] for item in body["items"]] == list(range(1, len(item_ids) + 1))
        # 関心が近い記事ほど上位（rank が小さい）
        related_rank = next(
            item["rank"] for item in body["items"] if item["article_id"] == str(related_article.id)
        )
        unrelated_rank = next(
            item["rank"]
            for item in body["items"]
            if item["article_id"] == str(unrelated_article.id)
        )
        assert related_rank < unrelated_rank
        assert body["mode"] == RecommendationMode.ARTICLE_BASED.value
        uuid.UUID(body["run_id"])

    def test_returns_404_for_an_unknown_article(self, client: TestClient) -> None:
        # Act
        response = client.post(f"/api/articles/{uuid.uuid4()}/recommendations")

        # Assert
        assert response.status_code == 404

    def test_does_not_include_the_article_body(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — ADR 0001: 記事本文は外部へ出さない
        source_article = make_article(db_session, title="起点記事", embedding=make_embedding(0))
        make_article(
            db_session,
            title="関連記事",
            body="これは内部限定の本文シークレットです",
            embedding=make_embedding(0),
        )

        # Act
        response = client.post(f"/api/articles/{source_article.id}/recommendations")

        # Assert
        assert response.status_code == 200
        for item in response.json()["items"]:
            assert "body" not in item
        assert "内部限定の本文シークレット" not in response.text


class TestGetFeed:
    def test_returns_up_to_the_default_page_size(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 既定 20 件（候補が少なければある分）を返す
        for index in range(5):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))

        # Act
        response = client.get("/api/feed")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 5

    def test_paginates_via_cursor_without_duplicates_and_with_consecutive_ranks(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: ページ間で記事の重複が無く、rank が連続する
        for index in range(5):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))

        # Act
        first_response = client.get("/api/feed", params={"limit": 2})
        first_body = first_response.json()
        second_response = client.get(
            "/api/feed", params={"limit": 2, "cursor": first_body["next_cursor"]}
        )
        second_body = second_response.json()

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        first_ids = {item["article_id"] for item in first_body["items"]}
        second_ids = {item["article_id"] for item in second_body["items"]}
        assert len(first_ids) == 2
        assert len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)
        assert [item["rank"] for item in first_body["items"]] == [1, 2]
        assert [item["rank"] for item in second_body["items"]] == [3, 4]
        assert first_body["next_cursor"] is not None

    def test_returns_a_null_next_cursor_when_there_is_no_more_data(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 次ページが無いとき next_cursor が null
        for index in range(2):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))

        # Act
        response = client.get("/api/feed", params={"limit": 2})

        # Assert
        assert response.status_code == 200
        assert response.json()["next_cursor"] is None

    def test_returns_400_for_a_malformed_cursor(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/feed", params={"cursor": "not-a-valid-cursor!!"})

        # Assert
        assert response.status_code == 400

    def test_returns_400_for_a_cursor_pointing_to_an_unknown_run(self, client: TestClient) -> None:
        # Arrange — 壊れていないが実在しない run_id を指す cursor
        bogus_cursor = base64.urlsafe_b64encode(f"{uuid.uuid4()}:1".encode()).decode().rstrip("=")

        # Act
        response = client.get("/api/feed", params={"cursor": bogus_cursor})

        # Assert
        assert response.status_code == 400

    def test_excludes_bad_articles(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: Bad 済み記事がフィードに出現しない
        good_article = make_article(db_session, title="良い記事", embedding=make_embedding(0))
        bad_article = make_article(db_session, title="Bad記事", embedding=make_embedding(1))
        add_bad_feedback(db_session, settings.default_user_id, bad_article)

        # Act
        response = client.get("/api/feed")

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(good_article.id) in item_ids
        assert str(bad_article.id) not in item_ids

    def test_reasons_in_the_response_match_the_stored_recommendation(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: スコア内訳が API レスポンスと DB の両方で確認できる
        make_article(db_session, title="候補", embedding=make_embedding(0), topics=["llm"])

        # Act
        response = client.get("/api/feed")

        # Assert
        item = response.json()["items"][0]
        assert "summary" in item["reasons"]
        assert "interest_similarity" in item["reasons"]
        assert "total" in item["reasons"]

        run = find_latest_run(db_session, settings.default_user_id, RecommendationMode.DISCOVER)
        assert run is not None
        stored = db_session.execute(
            select(Recommendation).where(
                Recommendation.run_id == run.id,
                Recommendation.article_id == uuid.UUID(item["article_id"]),
            )
        ).scalar_one()
        assert stored.reasons == item["reasons"]

    def test_does_not_include_the_article_body(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        make_article(
            db_session,
            title="候補",
            body="これは内部限定の本文シークレットです",
            embedding=make_embedding(0),
        )

        # Act
        response = client.get("/api/feed")

        # Assert
        for item in response.json()["items"]:
            assert "body" not in item
        assert "内部限定の本文シークレット" not in response.text

    @pytest.mark.parametrize("invalid_limit", [0, -1, 101])
    def test_rejects_an_out_of_range_limit(self, client: TestClient, invalid_limit: int) -> None:
        # Act
        response = client.get("/api/feed", params={"limit": invalid_limit})

        # Assert
        assert response.status_code == 422

    def test_returns_400_for_a_cursor_exceeding_the_max_length(self, client: TestClient) -> None:
        # Arrange — 受入基準: 上限を超える長さの cursor は 400
        oversized_cursor = "A" * (CURSOR_MAX_LENGTH + 1)

        # Act
        response = client.get("/api/feed", params={"cursor": oversized_cursor})

        # Assert
        assert response.status_code == 400

    def test_returns_400_for_a_cursor_with_an_oversized_rank(self, client: TestClient) -> None:
        # Arrange — 受入基準: rank 部分の桁数が異常に大きい cursor は 400
        # （int() へ渡す前に桁数で弾く）。桁数を増やすほど cursor 自体も長くなり、
        # 手前の長さチェックで弾かれて桁数チェックへ到達しなくなるため、
        # 上限をちょうど 1 桁だけ超える長さにする。
        raw = f"{uuid.uuid4()}:{'9' * (_MAX_CURSOR_RANK_DIGITS + 1)}"
        cursor = base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")
        assert len(cursor) <= CURSOR_MAX_LENGTH

        # Act
        response = client.get("/api/feed", params={"cursor": cursor})

        # Assert
        assert response.status_code == 400

    def test_returns_400_for_a_cursor_whose_run_id_is_not_a_uuid(self, client: TestClient) -> None:
        # Arrange — base64 としては復号できるが run_id が UUID ではない cursor
        raw = "not-a-uuid:1"
        cursor = base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")

        # Act
        response = client.get("/api/feed", params={"cursor": cursor})

        # Assert
        assert response.status_code == 400


class TestGetFeedRunReuse:
    """cursor 無しの GET /api/feed が run を無制限に生成しないことを検証する。"""

    def test_does_not_generate_a_new_run_within_the_reuse_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 短時間に 2 回叩いても run は 1 つしか作られない
        for index in range(3):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))
        client.app.dependency_overrides[get_now] = lambda: NOW

        # Act
        first_response = client.get("/api/feed")
        client.app.dependency_overrides[get_now] = lambda: NOW + timedelta(seconds=1)
        second_response = client.get("/api/feed")

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 1
        # 再利用時も返る items は 1 回目と同じ（rank 1 始まり）
        first_items = first_response.json()["items"]
        second_items = second_response.json()["items"]
        assert [item["article_id"] for item in first_items] == [
            item["article_id"] for item in second_items
        ]
        assert [item["rank"] for item in second_items] == list(range(1, len(second_items) + 1))

    def test_generates_a_new_run_after_the_reuse_window_expires(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 再利用期限を過ぎたら新しい run が作られる
        make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW
        client.get("/api/feed")
        reuse_seconds = get_scoring_config().limits.feed_run_reuse_seconds

        # Act
        client.app.dependency_overrides[get_now] = lambda: (
            NOW + timedelta(seconds=reuse_seconds + 1)
        )
        response = client.get("/api/feed")

        # Assert
        assert response.status_code == 200
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 2

    def test_always_generates_a_new_run_when_reuse_is_disabled(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange — 受入基準: feed_run_reuse_seconds=0 は常に新規生成を意味する
        config = get_scoring_config()
        disabled_config = config.model_copy(
            update={"limits": config.limits.model_copy(update={"feed_run_reuse_seconds": 0})}
        )
        monkeypatch.setattr(
            "techradar.api.recommendations.get_scoring_config", lambda: disabled_config
        )
        make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW

        # Act
        first_response = client.get("/api/feed")
        second_response = client.get("/api/feed")

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 2
