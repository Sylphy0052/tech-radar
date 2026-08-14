"""記事起点推薦と Discover フィードの API を検証する（`PROJECT_SPEC.md` §6.1, §13, §20）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.api.deps import get_now, get_session
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, Recommendation, RecommendationRun, UserArticle
from techradar.db.enums import ArticleOrigin, BadReason, FeedbackAction, RecommendationMode
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
    translated_title: str | None = None,
    summary_ja: str | None = None,
    body: str | None = None,
    source_domain: str = "example.com",
    topics: Sequence[str] = (),
    technologies: Sequence[str] = (),
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
        translated_title=translated_title,
        summary_ja=summary_ja,
        body=body,
        source_domain=source_domain,
        topics=list(topics),
        technologies=list(technologies),
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


def add_user_article(
    session: Session,
    user_id: uuid.UUID,
    article: Article,
    origin: ArticleOrigin,
    *,
    interest_weight: float = 1.0,
) -> None:
    """`user_articles` へ直接 origin を設定する（is_read 判定用テストのセットアップ）。"""
    session.add(
        UserArticle(
            user_id=user_id,
            article_id=article.id,
            origin=origin.value,
            interest_weight=interest_weight,
        )
    )
    session.flush()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def client(db_session: Session, settings: Settings) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。

    現在時刻も `NOW` へ固定する。このファイルの記事は `make_article` が
    `published_at`/`fetched_at` を `NOW` で作るため、実時刻のまま動かすと
    `NOW` から `freshness.max_age_days`（config/scoring.yaml）を過ぎた日に
    候補が全て絞り込みから外れ、テストが日付の経過だけで落ちる（Issue #48）。
    個別に時刻を動かしたいテストは、従来どおり `dependency_overrides` を
    上書きすればよい。
    """
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_now] = lambda: NOW
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

    def test_paginates_by_page_without_duplicates_and_with_consecutive_ranks(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: ページ間で記事の重複が無く、rank が連続する
        for index in range(5):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))

        # Act
        first_response = client.get("/api/feed", params={"limit": 2, "page": 1})
        second_response = client.get("/api/feed", params={"limit": 2, "page": 2})

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        first_body = first_response.json()
        second_body = second_response.json()
        first_ids = {item["article_id"] for item in first_body["items"]}
        second_ids = {item["article_id"] for item in second_body["items"]}
        assert len(first_ids) == 2
        assert len(second_ids) == 2
        assert first_ids.isdisjoint(second_ids)
        assert [item["rank"] for item in first_body["items"]] == [1, 2]
        assert [item["rank"] for item in second_body["items"]] == [3, 4]

    def test_returns_the_total_count_page_and_total_pages(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 総件数・総ページ数が正しい
        for index in range(5):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))

        # Act
        response = client.get("/api/feed", params={"limit": 2, "page": 2})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["total_count"] == 5
        assert body["page"] == 2
        assert body["page_size"] == 2
        assert body["total_pages"] == 3

    def test_returns_empty_items_but_correct_total_count_for_an_out_of_range_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 範囲外のページ番号はエラーではなく空の items
        for index in range(2):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))

        # Act
        response = client.get("/api/feed", params={"limit": 2, "page": 99})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total_count"] == 2
        assert body["page"] == 99
        assert body["total_pages"] == 1

    def test_returns_200_with_empty_items_when_no_candidates_match(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 条件に一致する候補が 0 件でも 500 にならない
        make_article(db_session, title="候補", embedding=make_embedding(0))

        # Act
        response = client.get("/api/feed", params={"q": "一致しない検索語"})

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total_count"] == 0
        assert body["total_pages"] == 0

    @pytest.mark.parametrize("invalid_page", [0, -1])
    def test_rejects_an_out_of_range_page(self, client: TestClient, invalid_page: int) -> None:
        # Act
        response = client.get("/api/feed", params={"page": invalid_page})

        # Assert
        assert response.status_code == 422

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


class TestGetFeedSearchAndFilters:
    """検索・絞り込み条件（Issue #90）を検証する。

    全候補を対象に推薦を作り直す方式のため、いずれのテストも 1 回目の
    `GET /api/feed` 呼び出しだけで完結する（`TestGetFeedRunReuse` のように
    ウィンドウ内で条件を変えずに読み直す手法とは異なる）。
    """

    def test_matches_the_search_term_in_the_title_case_insensitively(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 検索語が title に大文字小文字を区別せず部分一致する
        target = make_article(db_session, title="Python入門ガイド", embedding=make_embedding(0))
        other = make_article(db_session, title="Rustハンドブック", embedding=make_embedding(1))

        # Act
        response = client.get("/api/feed", params={"q": "python"})

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(target.id) in item_ids
        assert str(other.id) not in item_ids

    def test_matches_the_search_term_in_the_translated_title(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 検索語が translated_title に部分一致する
        target = make_article(
            db_session,
            title="Original",
            translated_title="日本語タイトル",
            embedding=make_embedding(0),
        )
        other = make_article(
            db_session, title="Other", translated_title="別の見出し", embedding=make_embedding(1)
        )

        # Act
        response = client.get("/api/feed", params={"q": "日本語"})

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(target.id) in item_ids
        assert str(other.id) not in item_ids

    def test_matches_the_search_term_in_the_summary(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 検索語が summary_ja に部分一致する
        target = make_article(
            db_session,
            title="記事A",
            summary_ja="LLMの活用事例を紹介する",
            embedding=make_embedding(0),
        )
        other = make_article(
            db_session, title="記事B", summary_ja="CSSレイアウトの基礎", embedding=make_embedding(1)
        )

        # Act
        response = client.get("/api/feed", params={"q": "LLM"})

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(target.id) in item_ids
        assert str(other.id) not in item_ids

    def test_requires_all_specified_topics(self, client: TestClient, db_session: Session) -> None:
        # Arrange — 受入基準: topics を複数指定したとき、全てを含む記事だけが残る
        both = make_article(
            db_session, title="両方持ち", topics=["llm", "rag"], embedding=make_embedding(0)
        )
        only_one = make_article(
            db_session, title="片方だけ", topics=["llm"], embedding=make_embedding(1)
        )

        # Act
        response = client.get("/api/feed", params={"topics": ["llm", "rag"]})

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(both.id) in item_ids
        assert str(only_one.id) not in item_ids

    def test_requires_all_specified_technologies(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: technologies を複数指定したとき、全てを含む記事だけが残る
        both = make_article(
            db_session,
            title="両方持ち",
            technologies=["python", "fastapi"],
            embedding=make_embedding(0),
        )
        only_one = make_article(
            db_session, title="片方だけ", technologies=["python"], embedding=make_embedding(1)
        )

        # Act
        response = client.get("/api/feed", params={"technologies": ["python", "fastapi"]})

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(both.id) in item_ids
        assert str(only_one.id) not in item_ids

    def test_filters_by_published_at_range(self, client: TestClient, db_session: Session) -> None:
        # Arrange — 受入基準: 公開日の範囲で絞れる
        in_range = make_article(
            db_session,
            title="範囲内",
            published_at=NOW - timedelta(days=1),
            embedding=make_embedding(0),
        )
        too_old = make_article(
            db_session,
            title="範囲外",
            published_at=NOW - timedelta(days=5),
            embedding=make_embedding(1),
        )

        # Act
        response = client.get(
            "/api/feed",
            params={
                "published_from": (NOW - timedelta(days=2)).isoformat(),
                "published_to": NOW.isoformat(),
            },
        )

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(in_range.id) in item_ids
        assert str(too_old.id) not in item_ids

    def test_filters_by_source_domain(self, client: TestClient, db_session: Session) -> None:
        # Arrange — 受入基準: 情報源ドメインで絞れる
        target = make_article(
            db_session,
            title="対象ドメイン",
            source_domain="example.com",
            embedding=make_embedding(0),
        )
        other = make_article(
            db_session,
            title="別ドメイン",
            source_domain="other.example",
            embedding=make_embedding(1),
        )

        # Act
        response = client.get("/api/feed", params={"source_domain": "example.com"})

        # Assert
        item_ids = [item["article_id"] for item in response.json()["items"]]
        assert str(target.id) in item_ids
        assert str(other.id) not in item_ids


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

    def test_excludes_an_article_badded_within_the_reuse_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準（Issue #13）: 再利用ウィンドウ内で Bad を付けた記事が

        再読み込み後のフィードから消える。

        Bad による候補除外は本来 `load_candidates` が新規 run を作るときにしか
        効かないため、再利用ウィンドウ内はレスポンス組み立て側の除外が無いと
        `feed_run_reuse_seconds`（最大 10 分）の間 Bad 記事が残り続けてしまう。
        """
        # Arrange
        article = make_article(db_session, title="Bad対象", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW
        first_response = client.get("/api/feed")
        assert [item["article_id"] for item in first_response.json()["items"]] == [str(article.id)]

        # Act — 再利用ウィンドウ内（get_now は変えない）で Bad を付けてから読み直す
        feedback_response = client.post(
            f"/api/articles/{article.id}/feedback", json={"action": "bad"}
        )
        second_response = client.get("/api/feed")

        # Assert
        assert feedback_response.status_code == 200
        # 同じ run が再利用され続けている（新規生成なら Bad はそもそも候補に
        # 含まれず、この挙動を検証できない）ことを確認する。
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 1
        assert second_response.json()["items"] == []

    def test_total_count_is_unaffected_by_badding_within_the_reuse_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        """API 契約: 総件数は run に入った件数であり、Bad 除外の影響を受けない。

        `total_count` は `recommendations` の保存件数から求める
        （`count_recommendations`）。Bad は表示時にレスポンス組み立て側
        （`_build_items`）が除外するだけで run の保存件数自体は変わらないため、
        再利用ウィンドウ内で Bad を付けても `total_count` は変化しない。
        クライアントは `items` の空だけでページングの終端と判断してはならない。
        """
        # Arrange
        for index in range(2):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))
        client.app.dependency_overrides[get_now] = lambda: NOW
        first_response = client.get("/api/feed", params={"limit": 1})
        first_items = first_response.json()["items"]
        assert len(first_items) == 1
        assert first_response.json()["total_count"] == 2

        # Act — 1 ページ目の記事を Bad にしてから同じページを読み直す
        client.post(
            f"/api/articles/{first_items[0]['article_id']}/feedback", json={"action": "bad"}
        )
        second_response = client.get("/api/feed", params={"limit": 1})

        # Assert — items は空だが、総件数・総ページ数は変わらない
        body = second_response.json()
        assert body["items"] == []
        assert body["total_count"] == 2
        assert body["total_pages"] == 2

    def test_reuses_the_same_run_for_the_same_filters(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 同じ条件で連続して呼ぶと同じ run が再利用される
        make_article(db_session, title="Python記事", topics=["llm"], embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW

        # Act
        first_response = client.get("/api/feed", params={"q": "python", "topics": ["llm"]})
        second_response = client.get("/api/feed", params={"q": "python", "topics": ["llm"]})

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 1

    def test_generates_a_new_run_when_filters_change(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: 条件を変えると別の run が作られる
        make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW

        # Act
        client.get("/api/feed", params={"q": "python"})
        client.get("/api/feed", params={"q": "rust"})

        # Assert
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 2

    def test_reuses_the_same_run_when_topic_order_differs(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: topics=a,b と topics=b,a は同じフィンガープリントになる
        make_article(db_session, title="候補", topics=["llm", "rag"], embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW

        # Act
        client.get("/api/feed", params={"topics": ["llm", "rag"]})
        client.get("/api/feed", params={"topics": ["rag", "llm"]})

        # Assert
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 1

    def test_no_filter_and_empty_filter_share_the_same_run(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 既存の条件なし呼び出しの挙動が壊れていないことも兼ねて確認する
        make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW

        # Act
        first_response = client.get("/api/feed")
        second_response = client.get("/api/feed")

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        run_count = db_session.scalar(select(func.count()).select_from(RecommendationRun))
        assert run_count == 1

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


class TestRecommendationItemFeedbackAndReadState:
    """`RecommendationItem` の `feedback` / `is_read`（Issue #13 T2）を検証する。

    Good/保存や既読状態は推薦生成後に追加されることが多いため、多くのケースで
    `feed_run_reuse_seconds`（`config/scoring.yaml`）の再利用ウィンドウ内に
    `get_now` を固定して同じ run を読み直す（`TestGetFeedRunReuse` と同じ手法）。
    再利用ウィンドウ内であれば `load_recommendation_page` が保存済みの
    recommendations をそのまま返すため、Good 由来の候補除外（`load_candidates`
    の `owned_exists`）の影響を受けずに、生成後に付いた最新の feedback / is_read
    を反映できることを確認できる。

    Bad は事情が異なる。再利用ウィンドウ内で Bad を付けた記事は `_build_items`
    がレスポンス組み立て時に除外するため（`TestGetFeedRunReuse.
    test_excludes_an_article_badded_within_the_reuse_window` 参照）、
    フィード項目としては現れなくなる。そのため「Bad（理由付き）が正しく記録
    されること」自体は、以下の `test_records_a_bad_feedback_with_a_reason_correctly`
    でフィードバック登録の応答（`POST` のレスポンス）に対して検証する。
    """

    def test_feed_item_has_no_feedback_and_is_unread_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        make_article(db_session, title="候補", embedding=make_embedding(0))

        # Act
        response = client.get("/api/feed")

        # Assert
        item = response.json()["items"][0]
        assert item["feedback"] is None
        assert item["is_read"] is False

    def test_feed_item_reflects_good_feedback_added_after_generation(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 受入基準: Good がリロード後も維持される
        article = make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW
        first_response = client.get("/api/feed")
        item_id = first_response.json()["items"][0]["article_id"]
        assert item_id == str(article.id)

        # Act — Good を付けてから再利用ウィンドウ内で読み直す
        feedback_response = client.post(
            f"/api/articles/{article.id}/feedback", json={"action": "good"}
        )
        second_response = client.get("/api/feed")

        # Assert
        assert feedback_response.status_code == 200
        item = second_response.json()["items"][0]
        assert item["feedback"]["action"] == "good"
        assert item["feedback"]["reason"] is None
        assert item["is_read"] is False

    def test_records_a_bad_feedback_with_a_reason_correctly(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: Bad（理由付き）が正しく記録される。

        以前はこの検証をフィード再取得後の item から行っていたが、Bad を
        付けた記事は再利用ウィンドウ内であってもフィードから消える
        （`TestGetFeedRunReuse.test_excludes_an_article_badded_within_the_reuse_window`）
        ため、記録内容そのものはフィードバック登録の応答に対して検証する。
        """
        # Arrange
        article = make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW
        client.get("/api/feed")

        # Act
        feedback_response = client.post(
            f"/api/articles/{article.id}/feedback",
            json={"action": "bad", "reason": BadReason.NOT_INTERESTED.value},
        )

        # Assert
        assert feedback_response.status_code == 200
        body = feedback_response.json()
        assert body["action"] == "bad"
        assert body["reason"] == BadReason.NOT_INTERESTED.value

    def test_feed_item_is_read_when_user_article_origin_is_read_full(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 受入基準: 既読記事に既読マークを表示する
        article = make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW
        client.get("/api/feed")
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.READ_FULL)

        # Act
        response = client.get("/api/feed")

        # Assert
        item = response.json()["items"][0]
        assert item["is_read"] is True

    def test_feed_item_is_unread_when_user_article_origin_is_manual(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 手動登録は既読判定の対象外（`READ_ORIGIN_VALUES` に含まれない）
        article = make_article(db_session, title="候補", embedding=make_embedding(0))
        client.app.dependency_overrides[get_now] = lambda: NOW
        client.get("/api/feed")
        add_user_article(db_session, settings.default_user_id, article, ArticleOrigin.MANUAL)

        # Act
        response = client.get("/api/feed")

        # Assert
        item = response.json()["items"][0]
        assert item["is_read"] is False

    def test_article_recommendations_include_feedback_and_is_read_fields(
        self, client: TestClient, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 記事起点推薦でも同じ項目が載ることを確認する
        source_article = make_article(
            db_session, title="起点記事", embedding=make_embedding(0), topics=["llm"]
        )
        related_article = make_article(
            db_session, title="近い記事", embedding=make_embedding(0), topics=["llm"]
        )
        # is_read は候補除外の対象外（owned とは異なる）なので、生成前に既読化しても
        # 候補には残る。
        add_user_article(
            db_session, settings.default_user_id, related_article, ArticleOrigin.READ_FULL
        )

        # Act
        response = client.post(f"/api/articles/{source_article.id}/recommendations")

        # Assert
        item = next(
            item
            for item in response.json()["items"]
            if item["article_id"] == str(related_article.id)
        )
        assert item["is_read"] is True
        assert item["feedback"] is None


class TestRecommendationRateLimit:
    """推薦 API のレート制限（Issue #28, `api/rate_limit.py`）を検証する。"""

    @pytest.fixture
    def rate_limited_client(self, db_session: Session) -> Iterator[TestClient]:
        """上限 2 回・ウィンドウ 60 秒に絞った設定でクライアントを組み立てる。

        既定値（30 回/60 秒）のままだと、上限超過を再現するために大量の
        リクエストを送る必要があり実時間の sleep 無しでは検証しづらい。
        """
        limited_settings = Settings(
            recommendation_rate_limit_requests=2,
            recommendation_rate_limit_window_seconds=60.0,
            _env_file=None,
        )
        app = create_app(limited_settings)
        app.dependency_overrides[get_session] = lambda: db_session
        app.dependency_overrides[get_now] = lambda: NOW
        with TestClient(app) as test_client:
            yield test_client
        app.dependency_overrides.clear()

    def test_returns_429_with_retry_after_when_the_feed_limit_is_exceeded(
        self, rate_limited_client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        make_article(db_session, title="候補", embedding=make_embedding(0))

        # Act — 上限（2 回）以内は通常応答、3 回目で 429
        first_response = rate_limited_client.get("/api/feed")
        second_response = rate_limited_client.get("/api/feed")
        third_response = rate_limited_client.get("/api/feed")

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert third_response.status_code == 429
        assert "Retry-After" in third_response.headers
        assert int(third_response.headers["Retry-After"]) >= 1

    def test_returns_429_with_retry_after_when_the_article_recommendations_limit_is_exceeded(
        self, rate_limited_client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        article = make_article(db_session, title="候補", embedding=make_embedding(0))

        # Act
        first_response = rate_limited_client.post(f"/api/articles/{article.id}/recommendations")
        second_response = rate_limited_client.post(f"/api/articles/{article.id}/recommendations")
        third_response = rate_limited_client.post(f"/api/articles/{article.id}/recommendations")

        # Assert
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert third_response.status_code == 429
        assert "Retry-After" in third_response.headers

    def test_allows_requests_within_the_limit_to_proceed_normally(
        self, rate_limited_client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        make_article(db_session, title="候補", embedding=make_embedding(0))

        # Act
        response = rate_limited_client.get("/api/feed")

        # Assert
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    def test_allows_requests_again_after_the_window_passes(
        self, rate_limited_client: TestClient, db_session: Session
    ) -> None:
        # Arrange — 上限（2 回）を使い切って 429 になることを確認する
        make_article(db_session, title="候補", embedding=make_embedding(0))
        rate_limited_client.get("/api/feed")
        rate_limited_client.get("/api/feed")
        assert rate_limited_client.get("/api/feed").status_code == 429

        # Act — ウィンドウ（60 秒）を過ぎてから読み直す
        rate_limited_client.app.dependency_overrides[get_now] = lambda: NOW + timedelta(seconds=61)
        response = rate_limited_client.get("/api/feed")

        # Assert
        assert response.status_code == 200
