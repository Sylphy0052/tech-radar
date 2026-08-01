"""URL 登録 API を検証する（`PROJECT_SPEC.md` §6.2, §20, Issue #12）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.db.models import ArticleRegistration, Job
from techradar.main import create_app


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。"""
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def independent_sessions(migrated_engine: Engine) -> Iterator[Callable[[], Session]]:
    """互いに独立した DB 接続をテスト内で必要な数だけ払い出す。

    `db_session` は1本の接続を共有し外側トランザクションでロールバックする作りのため、
    「コミット済みの行が別接続から見えるか」は再現できない。ここでは接続ごとに
    独立したトランザクションを張り、テストがコミットした行の後始末まで責任を持つ。
    """
    sessions: list[Session] = []

    def open_session() -> Session:
        session = Session(bind=migrated_engine, expire_on_commit=False)
        sessions.append(session)
        return session

    try:
        yield open_session
    finally:
        for session in sessions:
            session.close()
        with Session(bind=migrated_engine) as cleanup_session:
            cleanup_session.execute(delete(ArticleRegistration))
            cleanup_session.execute(delete(Job).where(Job.type == JobType.FETCH_ARTICLE.value))
            cleanup_session.commit()


class TestCreateArticleRegistration:
    def test_registers_a_url_as_pending_and_enqueues_a_fetch_job(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 登録すると 201 で pending が返り、fetch_article ジョブが1件積まれる。"""
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["url"] == "https://example.com/article"
        assert body["article_id"] is None
        assert body["error_reason"] is None
        uuid.UUID(body["id"])  # 有効な UUID 形式であること

        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_commits_the_registration_before_responding(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """受入基準: 応答を受け取った時点で、別接続からも登録を追跡できること。

        リクエスト単位のセッションは FastAPI の依存の後処理でコミットされるが、
        後処理が走るのはレスポンス送信より後になる。UI は応答直後に
        `GET /api/articles/registrations/{id}` を叩くため、コミットを応答前に
        済ませておかないと登録直後の状態取得が 404 になる。
        """
        # Arrange
        api_session = independent_sessions()
        observer = independent_sessions()
        app = create_app(Settings(_env_file=None))
        app.dependency_overrides[get_session] = lambda: api_session

        # Act
        with TestClient(app) as test_client:
            response = test_client.post("/api/articles", json={"url": "https://example.com/a"})

        # Assert — 応答時点で別接続から見えること（コミット済みであること）
        assert response.status_code == 201
        registration_id = uuid.UUID(response.json()["id"])
        assert observer.get(ArticleRegistration, registration_id) is not None

    def test_does_not_expose_normalized_url_or_user_id(self, client: TestClient) -> None:
        """内部情報（正規化 URL・ユーザー ID）を無用に露出させない。"""
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        body = response.json()
        assert "normalized_url" not in body
        assert "user_id" not in body

    def test_does_not_enqueue_a_second_job_when_the_same_url_is_registered_again(
        self, client: TestClient, db_session: Session
    ) -> None:
        """受入基準: 同じ URL を再登録しても 200 が返りジョブが増えない。"""
        # Arrange
        first_response = client.post("/api/articles", json={"url": "https://example.com/article"})
        first_id = first_response.json()["id"]

        # Act
        second_response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        assert second_response.status_code == 200
        assert second_response.json()["id"] == first_id
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_treats_urls_that_normalize_to_the_same_value_as_a_duplicate(
        self, client: TestClient, db_session: Session
    ) -> None:
        """正規化後に一致する別表記の URL でも重複登録として扱う。"""
        # Arrange
        first_response = client.post(
            "/api/articles", json={"url": "https://Example.com/article?utm_source=x"}
        )
        first_id = first_response.json()["id"]

        # Act — 末尾スラッシュ・大文字ホスト違いだけの別表記
        second_response = client.post("/api/articles", json={"url": "https://example.com/article/"})

        # Assert
        assert second_response.status_code == 200
        assert second_response.json()["id"] == first_id
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1

    def test_stores_the_registration_id_and_url_in_the_job_payload(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        registration_id = response.json()["id"]
        job = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).one()
        assert job.payload == {
            "registration_id": registration_id,
            "url": "https://example.com/article",
        }

    def test_links_the_created_job_to_the_registration(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Act
        response = client.post("/api/articles", json={"url": "https://example.com/article"})

        # Assert
        registration_id = uuid.UUID(response.json()["id"])
        registration = db_session.get(ArticleRegistration, registration_id)
        assert registration is not None
        job = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).one()
        assert registration.job_id == job.id

    def test_rejects_an_unknown_field(self, client: TestClient) -> None:
        # Act
        response = client.post(
            "/api/articles", json={"url": "https://example.com/article", "unexpected": "x"}
        )

        # Assert
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "invalid_url",
        [
            "ftp://example.com/article",
            "javascript:alert(1)",
            "not-a-url",
            "",
            "example.com/article",
        ],
    )
    def test_rejects_a_url_with_a_disallowed_scheme_or_invalid_format(
        self, client: TestClient, invalid_url: str
    ) -> None:
        """受入基準: http/https 以外のスキームや不正形式が 422 で弾かれる。"""
        # Act
        response = client.post("/api/articles", json={"url": invalid_url})

        # Assert
        assert response.status_code == 422

    def test_rejects_a_url_longer_than_the_configured_limit(self, client: TestClient) -> None:
        # Arrange
        too_long_url = "https://example.com/" + "a" * 2048

        # Act
        response = client.post("/api/articles", json={"url": too_long_url})

        # Assert
        assert response.status_code == 422


class TestGetArticleRegistration:
    def test_returns_the_registration_status(self, client: TestClient) -> None:
        # Arrange
        create_response = client.post("/api/articles", json={"url": "https://example.com/article"})
        registration_id = create_response.json()["id"]

        # Act
        response = client.get(f"/api/articles/registrations/{registration_id}")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == registration_id
        assert body["status"] == "pending"

    def test_returns_404_for_an_unknown_registration_id(self, client: TestClient) -> None:
        # Act
        response = client.get(f"/api/articles/registrations/{uuid.uuid4()}")

        # Assert
        assert response.status_code == 404

    def test_returns_404_for_a_registration_owned_by_another_user(
        self, client: TestClient, db_session: Session
    ) -> None:
        """他ユーザーの登録は参照させない。

        MVP は単一ユーザーのため現状は到達しない経路だが、作成側 (`POST`) が
        `user_id` で絞っているのに参照側が絞らないままだと、認証を導入した
        ときにこのエンドポイントだけ他人の登録を返してしまう。
        """
        # Arrange
        other_registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/other",
            normalized_url="https://example.com/other",
        )
        db_session.add(other_registration)
        db_session.flush()

        # Act
        response = client.get(f"/api/articles/registrations/{other_registration.id}")

        # Assert
        assert response.status_code == 404

    def test_does_not_expose_normalized_url_or_user_id(self, client: TestClient) -> None:
        # Arrange
        create_response = client.post("/api/articles", json={"url": "https://example.com/article"})
        registration_id = create_response.json()["id"]

        # Act
        response = client.get(f"/api/articles/registrations/{registration_id}")

        # Assert
        body = response.json()
        assert "normalized_url" not in body
        assert "user_id" not in body
