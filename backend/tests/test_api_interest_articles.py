"""関心記事一覧・フィルター・除外 API を検証する（`PROJECT_SPEC.md` §6.3, Issue #14）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.api.articles import (
    INTEREST_LIST_FILTER_MAX_ITEMS,
    INTEREST_LIST_TEXT_FILTER_MAX_LENGTH,
)
from techradar.api.deps import get_session
from techradar.api.query_filters import MAX_PAGE_NUMBER
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, UserArticle
from techradar.db.enums import ArticleOrigin, FeedbackAction
from techradar.main import create_app

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def make_article(
    session: Session,
    *,
    title: str = "記事タイトル",
    translated_title: str | None = None,
    summary_ja: str | None = None,
    topics: list[str] | None = None,
    technologies: list[str] | None = None,
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
        translated_title=translated_title,
        summary_ja=summary_ja,
        topics=topics if topics is not None else [],
        technologies=technologies if technologies is not None else [],
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


class TestListInterestArticlesSearch:
    """受入基準: 検索語が title / translated_title / summary_ja の部分一致で当たる（Issue #91）。"""

    def test_matches_the_original_title(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        hit = make_article(db_session, title="Rust の所有権")
        miss = make_article(db_session, title="Python の GIL")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "所有権"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_matches_the_translated_title(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        hit = make_article(db_session, title="Ownership in Rust", translated_title="Rust の所有権")
        miss = make_article(db_session, title="Python の GIL")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "所有権"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_matches_the_japanese_summary(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        hit = make_article(db_session, title="Ownership", summary_ja="所有権について解説する")
        miss = make_article(db_session, title="Python の GIL")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "所有権"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_ignores_case(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: 大文字小文字を区別しない
        article = make_article(db_session, title="Rust Ownership")
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "ownership"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(article.id)]

    def test_combines_the_query_with_other_filters_by_and(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 検索語に当たっても domain が違えば出ない
        hit = make_article(db_session, title="Rust 入門", domain="ai")
        miss = make_article(db_session, title="Rust 入門", domain="web")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "Rust", "domain": "ai"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_escapes_a_percent_in_the_search_term(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: `%` はワイルドカードではなくリテラルとして扱う（Issue #94）
        hit = make_article(db_session, title="割引100%還元")
        miss = make_article(db_session, title="割引100円還元")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "100%還元"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_escapes_an_underscore_in_the_search_term(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: `_` は任意の1文字ではなくリテラルとして扱う（Issue #94）
        hit = make_article(db_session, title="foo_bar入門")
        miss = make_article(db_session, title="fooXbar入門")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "foo_bar"})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_matches_a_search_term_containing_a_backslash_without_error(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: バックスラッシュを含む検索語で例外にならず記事にだけ当たる
        hit = make_article(db_session, title="C:\\Users\\example")
        miss = make_article(db_session, title="C:/Users/example")
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"q": "C:\\Users"})

        # Assert
        assert response.status_code == 200
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]


class TestListInterestArticlesTagFilters:
    """受入基準: topics / technologies は複数指定でき、指定した全てを含む記事に絞る（AND）。"""

    def test_filters_by_a_single_topic(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        hit = make_article(db_session, title="該当", topics=["LLM", "RAG"])
        miss = make_article(db_session, title="非該当", topics=["DB"])
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"topics": ["LLM"]})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]

    def test_requires_all_topics_when_multiple_are_given(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 片方しか持たない記事は落ちる（OR ではなく AND）
        both = make_article(db_session, title="両方", topics=["LLM", "RAG"])
        only_one = make_article(db_session, title="片方", topics=["LLM"])
        for article in (both, only_one):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"topics": ["LLM", "RAG"]})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(both.id)]

    def test_requires_all_technologies_when_multiple_are_given(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        both = make_article(db_session, title="両方", technologies=["Python", "FastAPI"])
        only_one = make_article(db_session, title="片方", technologies=["Python"])
        for article in (both, only_one):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"technologies": ["Python", "FastAPI"]})

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(both.id)]

    def test_combines_topics_and_technologies_by_and(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        hit = make_article(db_session, title="該当", topics=["LLM"], technologies=["Python"])
        miss = make_article(db_session, title="非該当", topics=["LLM"], technologies=["Go"])
        for article in (hit, miss):
            add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get(
            "/api/articles", params={"topics": ["LLM"], "technologies": ["Python"]}
        )

        # Assert
        assert [item["article_id"] for item in response.json()["items"]] == [str(hit.id)]


class TestListInterestArticlesPaging:
    """受入基準: 番号付きページングで総件数・総ページ数が返る（Issue #91）。"""

    def test_returns_the_requested_page_with_totals(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 5 件を 2 件ずつに割ると 3 ページ
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
        response = client.get("/api/articles", params={"page": 2, "limit": 2})

        # Assert — 登録日時の降順で 3 件目・4 件目
        body = response.json()
        expected_ids = [str(article.id) for article in reversed(articles)]
        assert [item["article_id"] for item in body["items"]] == expected_ids[2:4]
        assert body["total_count"] == 5
        assert body["page"] == 2
        assert body["page_size"] == 2
        assert body["total_pages"] == 3

    def test_paginates_without_duplicates_or_gaps(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 5 件を 2 件ずつ辿って全件が重複・欠落なく揃うことを確認する
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
        for page in range(1, 4):
            body = client.get("/api/articles", params={"page": page, "limit": 2}).json()
            collected_ids.extend(item["article_id"] for item in body["items"])

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
        for page in range(1, 4):
            body = client.get("/api/articles", params={"page": page, "limit": 2}).json()
            collected_ids.extend(item["article_id"] for item in body["items"])

        # Assert
        expected_ids = {str(article.id) for article in articles}
        assert set(collected_ids) == expected_ids
        assert len(collected_ids) == len(expected_ids)

    def test_counts_only_rows_matching_the_filters(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: 総件数はフィルター適用後の件数（ページ内の件数ではない）。"""
        # Arrange — domain=ai の5件だけが対象。domain=web は総件数にも入らない
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
        body = client.get("/api/articles", params={"page": 1, "limit": 2, "domain": "ai"}).json()

        # Assert
        assert body["total_count"] == 5
        assert body["total_pages"] == 3
        assert str(web_article.id) not in [item["article_id"] for item in body["items"]]

    def test_returns_empty_items_for_a_page_beyond_the_last(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: 範囲外のページはエラーにせず空で返す（`GET /api/feed` と同じ）。"""
        # Arrange
        article = make_article(db_session)
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"page": 99, "limit": 10})

        # Assert
        body = response.json()
        assert response.status_code == 200
        assert body["items"] == []
        assert body["total_count"] == 1
        assert body["total_pages"] == 1

    def test_returns_zero_totals_when_nothing_matches(self, client: TestClient) -> None:
        # Act — 1 件も無い状態
        body = client.get("/api/articles").json()

        # Assert — 0 件なら総ページ数も 0（切り上げで 1 にしない）
        assert body["items"] == []
        assert body["total_count"] == 0
        assert body["total_pages"] == 0

    def test_returns_zero_totals_for_an_origin_outside_the_interest_list(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        """一覧対象外の origin だけを指定したときも、ページング用の値が揃って返る。"""
        # Arrange
        article = make_article(db_session)
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/articles", params={"origin": ["read_full"]})

        # Assert
        body = response.json()
        assert response.status_code == 200
        assert body["items"] == []
        assert body["total_count"] == 0
        assert body["total_pages"] == 0
        assert body["page"] == 1

    def test_rejects_a_page_below_one(self, client: TestClient) -> None:
        # Act / Assert
        assert client.get("/api/articles", params={"page": 0}).status_code == 422

    def test_accepts_a_page_at_the_upper_bound(self, client: TestClient) -> None:
        """受入基準: page の上限ちょうどは 200 + 空の items（Issue #96）。"""
        # Act
        response = client.get("/api/articles", params={"page": MAX_PAGE_NUMBER})

        # Assert
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_rejects_a_page_above_the_upper_bound(self, client: TestClient) -> None:
        """受入基準: page の上限を超えると 422（Issue #96）。"""
        # Act / Assert
        response = client.get("/api/articles", params={"page": MAX_PAGE_NUMBER + 1})
        assert response.status_code == 422

    def test_rejects_a_page_that_would_overflow_bigint_offset(self, client: TestClient) -> None:
        """受入基準: bigint を超える offset になる page は 500 ではなく 422（Issue #96）。"""
        # Act / Assert
        response = client.get("/api/articles", params={"page": 10**19})
        assert response.status_code == 422


class TestListInterestArticlesInputLimits:
    """受入基準: 自由入力の絞り込み条件に上限を課す（`GET /api/feed` と同じ規則）。

    上限値は `api/articles.py` の定数から取り、テスト側で数値を二重管理しない。
    """

    def test_rejects_a_too_long_query(self, client: TestClient) -> None:
        # Act / Assert
        too_long = "a" * (INTEREST_LIST_TEXT_FILTER_MAX_LENGTH + 1)
        assert client.get("/api/articles", params={"q": too_long}).status_code == 422

    def test_rejects_too_many_topics(self, client: TestClient) -> None:
        # Act / Assert — 件数の上限は FastAPI の Query では表せないため関数内で検証している
        too_many = [f"topic{index}" for index in range(INTEREST_LIST_FILTER_MAX_ITEMS + 1)]
        assert client.get("/api/articles", params={"topics": too_many}).status_code == 422

    def test_rejects_a_too_long_topic_item(self, client: TestClient) -> None:
        # Act / Assert — 要素ごとの長さも Query では効かないため関数内で検証している
        too_long = "a" * (INTEREST_LIST_TEXT_FILTER_MAX_LENGTH + 1)
        assert client.get("/api/articles", params={"topics": [too_long]}).status_code == 422

    def test_rejects_too_many_technologies(self, client: TestClient) -> None:
        # Act / Assert
        too_many = [f"tech{index}" for index in range(INTEREST_LIST_FILTER_MAX_ITEMS + 1)]
        assert client.get("/api/articles", params={"technologies": too_many}).status_code == 422


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
