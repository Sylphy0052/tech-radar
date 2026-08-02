"""記事フィードバック API を検証する（`PROJECT_SPEC.md` §7, Issue #13）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Select, func, insert, select
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.api.feedback import (
    ArticleFeedbackCreate,
    ArticleFeedbackResponse,
    _upsert_feedback,
    _upsert_owned_user_article,
)
from techradar.config import Settings
from techradar.db import Article, ArticleFeedback, UserArticle
from techradar.db.enums import ArticleOrigin, BadReason, FeedbackAction
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

    def test_rejects_a_reason_when_the_action_is_not_bad(self, client: TestClient) -> None:
        """受入基準: reason は Bad 専用の任意項目（PROJECT_SPEC.md §7.2）。

        action=bad 以外で reason を送っても意味が無く、無意味な reason を
        保存させないため 422 で拒否する（Issue #13 自己レビュー D）。
        """
        # Act
        response = client.post(
            f"/api/articles/{uuid.uuid4()}/feedback",
            json={"action": "good", "reason": "too_shallow"},
        )

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

    def test_good_upgrades_an_existing_lighter_origin(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 既存より重みの大きい good で origin と重みが更新される。

        `_upsert_owned_user_article` の「既存の重みより大きければ origin と
        重みを更新する」分岐（Issue #13 自己レビュー C）。save（0.5）済みの記事へ
        続けて good（0.8）を送ると、user_articles が good・0.8 へ更新される。
        """
        # Arrange
        article = make_article(db_session)
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "save"})

        # Act
        client.post(f"/api/articles/{article.id}/feedback", json={"action": "good"})

        # Assert
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.GOOD.value
        assert user_article.interest_weight == pytest.approx(0.8)

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


class TestArticleFeedbackResponseSchema:
    """`ArticleFeedbackResponse` の型（Issue #13 自己レビュー B）を検証する。

    OpenAPI に enum を出し `frontend/src/lib/api-schema.d.ts` でもリテラル union に
    なるよう、`action` / `reason` を `str` ではなく `FeedbackAction` / `BadReason`
    にしている。DB の `action` / `reason` 列は text 型のため、pydantic が
    `from_attributes` 経由で正しく enum 型へ変換できることを確認する。
    """

    def test_converts_the_db_text_columns_into_enum_members(self, db_session: Session) -> None:
        # Arrange
        article = make_article(db_session)
        feedback = ArticleFeedback(
            user_id=uuid.uuid4(),
            article_id=article.id,
            action="bad",
            reason="too_shallow",
        )
        db_session.add(feedback)
        db_session.flush()

        # Act
        response_model = ArticleFeedbackResponse.model_validate(feedback)

        # Assert
        assert response_model.action is FeedbackAction.BAD
        assert response_model.reason is BadReason.TOO_SHALLOW


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


class TestUpsertConcurrency:
    """`_upsert_feedback` / `_upsert_owned_user_article` の TOCTOU 対応

    （Issue #13 自己レビュー E）を検証する。

    「存在確認（`session.get` / `session.scalar`）→ INSERT」の間に別リクエストが
    同じ行を先に作る二重クリック等を、Core insert で ORM の識別マップを経由せず
    先に行を作ったうえで、対象関数自身の最初の存在確認だけ「まだ無い」ふりを
    することで再現する。この後の INSERT は本物の一意制約違反（SQLSTATE 23505）
    になるため、`is_unique_violation` の判定も実物の DB 例外で検証できる。
    """

    def test_upsert_feedback_recovers_from_a_concurrent_insert(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 同一記事への同時 POST（二重クリック等）で 500 にならない。"""
        # Arrange — 「別リクエストが先に作った」行を ORM を経由せず直接挿入する
        article = make_article(db_session)
        user_id = settings.default_user_id
        db_session.execute(
            insert(ArticleFeedback).values(
                user_id=user_id, article_id=article.id, action="good", reason=None
            )
        )
        db_session.flush()

        original_get = db_session.get
        already_skipped = False

        def _get_missing_once(
            entity: type[ArticleFeedback], ident: tuple[uuid.UUID, uuid.UUID]
        ) -> ArticleFeedback | None:
            nonlocal already_skipped
            if not already_skipped:
                already_skipped = True
                return None
            return original_get(entity, ident)

        monkeypatch.setattr(db_session, "get", _get_missing_once)
        payload = ArticleFeedbackCreate(action=FeedbackAction.BAD, reason=BadReason.TOO_SHALLOW)

        # Act
        feedback = _upsert_feedback(db_session, user_id, article.id, payload)

        # Assert — 500 にならず、既存行を読み直して bad に更新できている（1 行のまま）
        assert feedback.action == "bad"
        assert feedback.reason == "too_shallow"
        row_count = db_session.scalar(
            select(func.count())
            .select_from(ArticleFeedback)
            .where(ArticleFeedback.user_id == user_id, ArticleFeedback.article_id == article.id)
        )
        assert row_count == 1

    def test_upsert_owned_user_article_recovers_from_a_concurrent_insert(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: `user_articles` 側でも同時 INSERT の衝突で 500 にならない。

        「先に作られていた」行の重み（save=0.5）より大きい重み（good=0.8）で
        upsert するため、TOCTOU からの復帰後も C のアップグレード分岐が
        正しく適用されることも合わせて確認する。
        """
        # Arrange
        article = make_article(db_session)
        user_id = settings.default_user_id
        db_session.execute(
            insert(UserArticle).values(
                user_id=user_id,
                article_id=article.id,
                origin=ArticleOrigin.SAVED.value,
                interest_weight=0.5,
            )
        )
        db_session.flush()

        original_scalar = db_session.scalar
        already_skipped = False

        def _scalar_missing_once(statement: Select[tuple[UserArticle]]) -> UserArticle | None:
            nonlocal already_skipped
            if not already_skipped:
                already_skipped = True
                return None
            return original_scalar(statement)

        monkeypatch.setattr(db_session, "scalar", _scalar_missing_once)

        # Act
        _upsert_owned_user_article(db_session, user_id, article.id, ArticleOrigin.GOOD, 0.8)

        # Assert
        user_article = _get_user_article(db_session, article.id)
        assert user_article is not None
        assert user_article.origin == ArticleOrigin.GOOD.value
        assert user_article.interest_weight == pytest.approx(0.8)
