"""関心記事一覧・フィルター・除外 API を検証する（`PROJECT_SPEC.md` §6.3, Issue #14）。"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.api.articles import (
    INTEREST_CURSOR_MAX_LENGTH,
    INTEREST_LIST_TEXT_FILTER_MAX_LENGTH,
)
from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, UserArticle
from techradar.db.enums import ArticleOrigin, FeedbackAction
from techradar.main import create_app

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def make_article(
    session: Session,
    *,
    title: str = "記事タイトル",
    source_domain: str = "example.com",
    language: str | None = "ja",
    domain: str | None = "engineering",
    category: str | None = "backend",
    content_type: str | None = "implementation",
    is_primary_source: bool = False,
    published_at: datetime | None = NOW,
) -> Article:
    """関心記事一覧の対象として使う記事を DB へ保存する。"""
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title=title,
        source_domain=source_domain,
        language=language,
        domain=domain,
        category=category,
        content_type=content_type,
        is_primary_source=is_primary_source,
        published_at=published_at,
        fetched_at=NOW,
    )
    session.add(article)
    session.flush()
    return article


def add_user_article(
    session: Session,
    user_id: uuid.UUID,
    article: Article,
    origin: ArticleOrigin,
    *,
    created_at: datetime = NOW,
    interest_weight: float = 1.0,
) -> UserArticle:
    """`user_articles` へ直接 origin・登録日時を設定する。"""
    user_article = UserArticle(
        user_id=user_id,
        article_id=article.id,
        origin=origin.value,
        interest_weight=interest_weight,
        created_at=created_at,
    )
    session.add(user_article)
    session.flush()
    return user_article


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


class TestListInterestArticles:
    def test_returns_articles_registered_via_the_three_origins_with_their_origin(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: manual / good / saved の3経路の記事が登録方法付きで返る。"""
        # Arrange
        manual_article = make_article(db_session, title="手動登録記事")
        good_article = make_article(db_session, title="Good記事")
        saved_article = make_article(db_session, title="保存記事")
        add_user_article(
            db_session,
            settings.default_user_id,
            manual_article,
            ArticleOrigin.MANUAL,
            created_at=NOW,
        )
        add_user_article(
            db_session,
            settings.default_user_id,
            good_article,
            ArticleOrigin.GOOD,
            created_at=NOW + timedelta(minutes=1),
        )
        add_user_article(
            db_session,
            settings.default_user_id,
            saved_article,
            ArticleOrigin.SAVED,
            created_at=NOW + timedelta(minutes=2),
        )

        # Act
        response = client.get("/api/articles")

        # Assert
        assert response.status_code == 200
        body = response.json()
        origins_by_article_id = {item["article_id"]: item["origin"] for item in body["items"]}
        assert origins_by_article_id == {
            str(manual_article.id): "manual",
            str(good_article.id): "good",
            str(saved_article.id): "saved",
        }

    def test_excludes_read_full_and_clicked_origins(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: read_full / clicked は一覧に出さない。"""
        # Arrange
        read_article = make_article(db_session, title="全文閲覧記事")
        clicked_article = make_article(db_session, title="クリック記事")
        add_user_article(
            db_session, settings.default_user_id, read_article, ArticleOrigin.READ_FULL
        )
        add_user_article(
            db_session, settings.default_user_id, clicked_article, ArticleOrigin.CLICKED
        )

        # Act
        response = client.get("/api/articles")

        # Assert
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_orders_items_by_registration_time_descending(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        older = make_article(db_session, title="古い記事")
        newer = make_article(db_session, title="新しい記事")
        add_user_article(
            db_session, settings.default_user_id, older, ArticleOrigin.MANUAL, created_at=NOW
        )
        add_user_article(
            db_session,
            settings.default_user_id,
            newer,
            ArticleOrigin.MANUAL,
            created_at=NOW + timedelta(minutes=5),
        )

        # Act
        response = client.get("/api/articles")

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(newer.id), str(older.id)]

    def test_returns_response_fields_from_both_user_article_and_article(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: レスポンス項目一式が返る。"""
        # Arrange
        article = make_article(
            db_session,
            title="記事タイトル",
            source_domain="blog.example.com",
            language="en",
            domain="ai",
            category="llm",
            content_type="research",
            is_primary_source=True,
        )
        add_user_article(
            db_session, settings.default_user_id, article, ArticleOrigin.MANUAL, created_at=NOW
        )

        # Act
        response = client.get("/api/articles")

        # Assert
        item = response.json()["items"][0]
        assert item["article_id"] == str(article.id)
        assert item["origin"] == "manual"
        assert item["registered_at"] is not None
        assert item["title"] == "記事タイトル"
        assert item["translated_title"] is None
        assert item["canonical_url"] == article.canonical_url
        assert item["original_url"] == article.original_url
        assert item["source_domain"] == "blog.example.com"
        assert item["language"] == "en"
        assert item["topics"] == []
        assert item["domain"] == "ai"
        assert item["category"] == "llm"
        assert item["content_type"] == "research"
        assert item["is_primary_source"] is True
        assert item["published_at"] is not None

    def test_does_not_include_another_users_articles(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 他ユーザーのデータが混ざらない。"""
        # Arrange
        other_article = make_article(db_session, title="他人の記事")
        add_user_article(db_session, uuid.uuid4(), other_article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles")

        # Assert
        assert response.json()["items"] == []


class TestListInterestArticlesFilters:
    def test_filters_by_origin_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        good_article = make_article(db_session, title="Good記事")
        saved_article = make_article(db_session, title="保存記事")
        add_user_article(db_session, settings.default_user_id, good_article, ArticleOrigin.GOOD)
        add_user_article(db_session, settings.default_user_id, saved_article, ArticleOrigin.SAVED)

        # Act
        response = client.get("/api/articles", params={"origin": "good"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(good_article.id)]

    def test_filters_by_multiple_origins(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        manual_article = make_article(db_session, title="手動")
        good_article = make_article(db_session, title="Good")
        saved_article = make_article(db_session, title="保存")
        add_user_article(db_session, settings.default_user_id, manual_article, ArticleOrigin.MANUAL)
        add_user_article(db_session, settings.default_user_id, good_article, ArticleOrigin.GOOD)
        add_user_article(db_session, settings.default_user_id, saved_article, ArticleOrigin.SAVED)

        # Act
        response = client.get("/api/articles", params=[("origin", "good"), ("origin", "saved")])

        # Assert
        ids = {item["article_id"] for item in response.json()["items"]}
        assert ids == {str(good_article.id), str(saved_article.id)}

    def test_filters_by_domain_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        ai_article = make_article(db_session, title="AI記事", domain="ai")
        web_article = make_article(db_session, title="Web記事", domain="web")
        add_user_article(db_session, settings.default_user_id, ai_article, ArticleOrigin.MANUAL)
        add_user_article(db_session, settings.default_user_id, web_article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"domain": "ai"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(ai_article.id)]

    def test_filters_by_category_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        llm_article = make_article(db_session, title="LLM記事", category="llm")
        infra_article = make_article(db_session, title="Infra記事", category="infra")
        add_user_article(db_session, settings.default_user_id, llm_article, ArticleOrigin.MANUAL)
        add_user_article(db_session, settings.default_user_id, infra_article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"category": "llm"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(llm_article.id)]

    def test_filters_by_source_domain_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        a_article = make_article(db_session, title="A", source_domain="a.example.com")
        b_article = make_article(db_session, title="B", source_domain="b.example.com")
        add_user_article(db_session, settings.default_user_id, a_article, ArticleOrigin.MANUAL)
        add_user_article(db_session, settings.default_user_id, b_article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"source_domain": "a.example.com"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(a_article.id)]

    def test_filters_by_language_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        ja_article = make_article(db_session, title="日本語記事", language="ja")
        en_article = make_article(db_session, title="English article", language="en")
        add_user_article(db_session, settings.default_user_id, ja_article, ArticleOrigin.MANUAL)
        add_user_article(db_session, settings.default_user_id, en_article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"language": "en"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(en_article.id)]

    def test_filters_by_is_primary_source_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        primary_article = make_article(db_session, title="一次情報", is_primary_source=True)
        secondary_article = make_article(db_session, title="解説記事", is_primary_source=False)
        add_user_article(
            db_session, settings.default_user_id, primary_article, ArticleOrigin.MANUAL
        )
        add_user_article(
            db_session, settings.default_user_id, secondary_article, ArticleOrigin.MANUAL
        )

        # Act
        response = client.get("/api/articles", params={"is_primary_source": "true"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(primary_article.id)]

    def test_filters_by_registered_period_alone(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        before = make_article(db_session, title="範囲外(前)")
        within = make_article(db_session, title="範囲内")
        after = make_article(db_session, title="範囲外(後)")
        add_user_article(
            db_session,
            settings.default_user_id,
            before,
            ArticleOrigin.MANUAL,
            created_at=NOW - timedelta(days=1),
        )
        add_user_article(
            db_session, settings.default_user_id, within, ArticleOrigin.MANUAL, created_at=NOW
        )
        add_user_article(
            db_session,
            settings.default_user_id,
            after,
            ArticleOrigin.MANUAL,
            created_at=NOW + timedelta(days=2),
        )

        # Act
        response = client.get(
            "/api/articles",
            params={
                "registered_from": NOW.isoformat(),
                "registered_to": (NOW + timedelta(days=1)).isoformat(),
            },
        )

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(within.id)]

    def test_registered_period_boundaries_are_inclusive(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: registered_from / registered_to の境界値を含む。"""
        # Arrange
        at_from = make_article(db_session, title="fromちょうど")
        at_to = make_article(db_session, title="toちょうど")
        registered_to = NOW + timedelta(days=1)
        add_user_article(
            db_session, settings.default_user_id, at_from, ArticleOrigin.MANUAL, created_at=NOW
        )
        add_user_article(
            db_session,
            settings.default_user_id,
            at_to,
            ArticleOrigin.MANUAL,
            created_at=registered_to,
        )

        # Act
        response = client.get(
            "/api/articles",
            params={"registered_from": NOW.isoformat(), "registered_to": registered_to.isoformat()},
        )

        # Assert
        ids = {item["article_id"] for item in response.json()["items"]}
        assert ids == {str(at_from.id), str(at_to.id)}

    def test_combines_multiple_filters_with_and(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: 複数フィルターの組み合わせが AND で効く。"""
        # Arrange — origin=good かつ domain=ai の1件だけが合致する
        matching = make_article(db_session, title="合致", domain="ai")
        wrong_origin = make_article(db_session, title="origin違い", domain="ai")
        wrong_domain = make_article(db_session, title="domain違い", domain="web")
        add_user_article(db_session, settings.default_user_id, matching, ArticleOrigin.GOOD)
        add_user_article(db_session, settings.default_user_id, wrong_origin, ArticleOrigin.SAVED)
        add_user_article(db_session, settings.default_user_id, wrong_domain, ArticleOrigin.GOOD)

        # Act
        response = client.get("/api/articles", params={"origin": "good", "domain": "ai"})

        # Assert
        items = response.json()["items"]
        assert [item["article_id"] for item in items] == [str(matching.id)]

    def test_returns_200_with_empty_items_for_an_origin_outside_the_interest_list(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """設計判断: read_full/clicked のみを指定しても 422 ではなく 200 + 空配列を返す。"""
        # Arrange
        article = make_article(db_session, title="全文閲覧記事")
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.READ_FULL)

        # Act
        response = client.get("/api/articles", params={"origin": "read_full"})

        # Assert
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_rejects_an_undefined_origin_value(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/articles", params={"origin": "not_a_real_origin"})

        # Assert
        assert response.status_code == 422

    def test_rejects_a_registered_from_after_registered_to(self, client: TestClient) -> None:
        # Act
        response = client.get(
            "/api/articles",
            params={
                "registered_from": (NOW + timedelta(days=1)).isoformat(),
                "registered_to": NOW.isoformat(),
            },
        )

        # Assert
        assert response.status_code == 422

    def test_rejects_a_naive_registered_from(self, client: TestClient) -> None:
        """受入基準: タイムゾーン無しの registered_from は 422 で拒否する。"""
        # Act — オフセット無し（naive）の日時文字列
        response = client.get("/api/articles", params={"registered_from": "2026-08-01T00:00:00"})

        # Assert
        assert response.status_code == 422

    def test_rejects_a_naive_registered_to(self, client: TestClient) -> None:
        """受入基準: タイムゾーン無しの registered_to は 422 で拒否する。"""
        # Act
        response = client.get("/api/articles", params={"registered_to": "2026-08-01T00:00:00"})

        # Assert
        assert response.status_code == 422

    def test_rejects_a_domain_exceeding_the_max_length(self, client: TestClient) -> None:
        # Arrange
        oversized_domain = "a" * (INTEREST_LIST_TEXT_FILTER_MAX_LENGTH + 1)

        # Act
        response = client.get("/api/articles", params={"domain": oversized_domain})

        # Assert
        assert response.status_code == 422


class TestListInterestArticlesPaging:
    def test_paginates_without_duplicates_or_gaps(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 5 件を作り、2 件ずつページングして全件が重複・欠落なく揃うことを確認する
        articles = [make_article(db_session, title=f"記事{index}") for index in range(5)]
        for index, article in enumerate(articles):
            add_user_article(
                db_session,
                settings.default_user_id,
                article,
                ArticleOrigin.MANUAL,
                created_at=NOW + timedelta(minutes=index),
            )

        # Act
        collected_ids: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            params = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/api/articles", params=params)
            body = response.json()
            collected_ids.extend(item["article_id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        # Assert
        expected_ids = [str(article.id) for article in reversed(articles)]
        assert collected_ids == expected_ids
        assert len(set(collected_ids)) == len(collected_ids)

    def test_paginates_correctly_when_created_at_is_identical_for_multiple_rows(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: created_at が同一でも user_articles.id でタイブレークし、重複・欠落が無い。"""
        # Arrange — 5 件すべて同一の created_at にする
        # （分単位でずらすと id によるタイブレークが検証できない）
        articles = [make_article(db_session, title=f"同時刻記事{index}") for index in range(5)]
        for article in articles:
            add_user_article(
                db_session, settings.default_user_id, article, ArticleOrigin.MANUAL, created_at=NOW
            )

        # Act
        collected_ids: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            params = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/api/articles", params=params)
            body = response.json()
            collected_ids.extend(item["article_id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        # Assert
        expected_ids = {str(article.id) for article in articles}
        assert set(collected_ids) == expected_ids
        assert len(collected_ids) == len(expected_ids)

    def test_paginates_correctly_when_a_filter_is_combined_with_cursor_and_limit(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: フィルター（domain）と cursor/limit を併用しても正しく分割・復元される。"""
        # Arrange — domain=ai の5件だけをページングで回収する。domain=web は紛れ込まない
        ai_articles = [
            make_article(db_session, title=f"AI記事{index}", domain="ai") for index in range(5)
        ]
        web_article = make_article(db_session, title="Web記事", domain="web")
        for index, article in enumerate(ai_articles):
            add_user_article(
                db_session,
                settings.default_user_id,
                article,
                ArticleOrigin.MANUAL,
                created_at=NOW + timedelta(minutes=index),
            )
        add_user_article(
            db_session,
            settings.default_user_id,
            web_article,
            ArticleOrigin.MANUAL,
            created_at=NOW + timedelta(minutes=10),
        )

        # Act
        collected_ids: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, str | int] = {"limit": 2, "domain": "ai"}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/api/articles", params=params)
            body = response.json()
            collected_ids.extend(item["article_id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        # Assert
        expected_ids = [str(article.id) for article in reversed(ai_articles)]
        assert collected_ids == expected_ids
        assert str(web_article.id) not in collected_ids

    def test_returns_a_null_next_cursor_when_there_is_no_more_data(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"limit": 10})

        # Assert
        assert response.json()["next_cursor"] is None

    def test_returns_400_for_a_malformed_cursor(self, client: TestClient) -> None:
        # Act
        response = client.get("/api/articles", params={"cursor": "not-a-valid-cursor!!"})

        # Assert
        assert response.status_code == 400

    def test_returns_400_for_a_cursor_exceeding_the_max_length(self, client: TestClient) -> None:
        # Arrange — 受入基準: 上限を超える長さの cursor は 400
        oversized_cursor = "A" * (INTEREST_CURSOR_MAX_LENGTH + 1)

        # Act
        response = client.get("/api/articles", params={"cursor": oversized_cursor})

        # Assert
        assert response.status_code == 400

    def test_returns_400_for_a_cursor_with_an_unparseable_created_at_or_id(
        self, client: TestClient
    ) -> None:
        # Arrange — base64 としては復号できるが、created_at/id として不正な cursor
        raw = "not-a-datetime:not-a-uuid"
        cursor = base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")

        # Act
        response = client.get("/api/articles", params={"cursor": cursor})

        # Assert
        assert response.status_code == 400


class TestDeleteInterestArticle:
    def test_removes_the_user_article_row(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        article = make_article(db_session)
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.delete(f"/api/articles/{article.id}/interest")

        # Assert
        assert response.status_code == 204
        remaining = db_session.scalar(
            select(UserArticle).where(
                UserArticle.user_id == settings.default_user_id,
                UserArticle.article_id == article.id,
            )
        )
        assert remaining is None

    def test_does_not_touch_article_feedback(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: 除外操作は article_feedback に触れない（Bad も付けない）。"""
        # Arrange
        article = make_article(db_session)
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.GOOD)
        db_session.add(
            ArticleFeedback(
                user_id=settings.default_user_id,
                article_id=article.id,
                action=FeedbackAction.GOOD.value,
            )
        )
        db_session.flush()

        # Act
        response = client.delete(f"/api/articles/{article.id}/interest")

        # Assert
        assert response.status_code == 204
        feedback = db_session.get(ArticleFeedback, (settings.default_user_id, article.id))
        assert feedback is not None
        assert feedback.action == FeedbackAction.GOOD.value

    def test_returns_404_when_no_row_exists(self, client: TestClient, db_session: Session) -> None:
        # Arrange
        article = make_article(db_session)

        # Act
        response = client.delete(f"/api/articles/{article.id}/interest")

        # Assert
        assert response.status_code == 404

    def test_does_not_remove_another_users_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 他ユーザーのデータが混ざらない（除外は自分の行にしか効かない）。"""
        # Arrange
        article = make_article(db_session)
        other_user_id = uuid.uuid4()
        add_user_article(db_session, other_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.delete(f"/api/articles/{article.id}/interest")

        # Assert
        assert response.status_code == 404
        remaining = db_session.scalar(
            select(UserArticle).where(
                UserArticle.user_id == other_user_id, UserArticle.article_id == article.id
            )
        )
        assert remaining is not None
