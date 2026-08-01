"""ジョブの進捗取得 API を検証する（`PROJECT_SPEC.md` §20）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.jobs.queue import enqueue
from techradar.main import create_app


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。"""
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestGetJob:
    def test_returns_the_status_and_attempts_of_an_enqueued_job(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange
        job = enqueue(db_session, JobType.CRAWL_SOURCES, {"source_domain": "example.com"})
        db_session.flush()

        # Act
        response = client.get(f"/api/jobs/{job.id}")

        # Assert
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(job.id)
        assert body["status"] == job.status
        assert body["attempts"] == 0

    def test_returns_404_for_an_unknown_job_id(self, client: TestClient) -> None:
        # Arrange / Act
        response = client.get(f"/api/jobs/{uuid.uuid4()}")

        # Assert
        assert response.status_code == 404

    def test_rejects_a_malformed_job_id(self, client: TestClient) -> None:
        # Arrange / Act
        response = client.get("/api/jobs/not-a-uuid")

        # Assert
        assert response.status_code == 422

    def test_does_not_expose_the_payload(self, client: TestClient, db_session: Session) -> None:
        # Arrange — payload には将来 URL 等の内部情報が入りうるため露出させない
        job = enqueue(db_session, JobType.CRAWL_SOURCES, {"source_domain": "secret.example.com"})
        db_session.flush()

        # Act
        response = client.get(f"/api/jobs/{job.id}")

        # Assert
        assert "payload" not in response.json()

    def test_does_not_expose_the_last_error(self, client: TestClient, db_session: Session) -> None:
        """HIGH: last_error は例外メッセージに URL 等の内部情報が入りうるため露出させない。"""
        # Arrange
        job = enqueue(db_session, JobType.FETCH_ARTICLE)
        job.last_error = "https://internal.example.com/secret?api_key=xxxxx"
        db_session.flush()

        # Act
        response = client.get(f"/api/jobs/{job.id}")

        # Assert
        assert "last_error" not in response.json()
