"""巡回ジョブの起動 API を検証する（`PROJECT_SPEC.md` §20、Issue #8）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.db.models import Job
from techradar.main import create_app


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。"""
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestCreateCrawlRun:
    def test_enqueues_a_job_that_can_be_tracked_via_the_jobs_api(self, client: TestClient) -> None:
        """受入基準: 返した job_id で GET /api/jobs/{job_id} を追えること。"""
        # Act
        create_response = client.post("/api/crawl/runs")

        # Assert
        assert create_response.status_code == 201
        body = create_response.json()
        job_id = body["job_id"]
        uuid.UUID(job_id)  # 有効な UUID 形式であること
        assert body["status"]

        track_response = client.get(f"/api/jobs/{job_id}")
        assert track_response.status_code == 200
        assert track_response.json()["id"] == job_id

    def test_allows_an_omitted_body(self, client: TestClient) -> None:
        # Act
        response = client.post("/api/crawl/runs")

        # Assert
        assert response.status_code == 201

    def test_stores_the_source_domain_in_the_job_payload(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange / Act
        response = client.post("/api/crawl/runs", json={"source_domain": "example.com"})

        # Assert
        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["job_id"])
        job = db_session.get(Job, job_id)
        assert job is not None
        assert job.type == JobType.CRAWL_SOURCES.value
        assert job.payload == {"source_domain": "example.com"}

    def test_rejects_an_unknown_field(self, client: TestClient) -> None:
        # Act
        response = client.post("/api/crawl/runs", json={"unexpected_field": "x"})

        # Assert
        assert response.status_code == 422

    def test_does_not_persist_when_source_domain_is_omitted(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange / Act
        response = client.post("/api/crawl/runs")

        # Assert — 空 payload のまま積まれる
        job_id = uuid.UUID(response.json()["job_id"])
        job = db_session.get(Job, job_id)
        assert job is not None
        assert job.payload == {}

    def test_only_matching_jobs_are_created(self, client: TestClient, db_session: Session) -> None:
        # Arrange / Act
        client.post("/api/crawl/runs")

        # Assert
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.CRAWL_SOURCES.value)).all()
        assert len(jobs) == 1
