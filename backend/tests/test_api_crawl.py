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
from techradar.jobs.queue import claim_next, complete
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

    def test_enqueues_with_an_empty_payload_when_source_domain_is_omitted(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange / Act
        response = client.post("/api/crawl/runs")

        # Assert — 空 payload のまま積まれる
        job_id = uuid.UUID(response.json()["job_id"])
        job = db_session.get(Job, job_id)
        assert job is not None
        assert job.payload == {}

    def test_creates_exactly_one_job_per_request(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange / Act
        client.post("/api/crawl/runs")

        # Assert
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.CRAWL_SOURCES.value)).all()
        assert len(jobs) == 1

    def test_returns_the_existing_job_when_a_pending_crawl_job_already_exists(
        self, client: TestClient, db_session: Session
    ) -> None:
        """MEDIUM: 連打しても検索 API / LLM 呼び出しが積み上がらないよう重複起動を防ぐ。"""
        # Arrange
        first_response = client.post("/api/crawl/runs")
        first_job_id = first_response.json()["job_id"]

        # Act
        second_response = client.post("/api/crawl/runs")

        # Assert
        assert second_response.status_code == 200
        assert second_response.json()["job_id"] == first_job_id
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.CRAWL_SOURCES.value)).all()
        assert len(jobs) == 1

    def test_returns_the_existing_job_when_a_running_crawl_job_already_exists(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange: pending -> 実行中 status(searching) に遷移させる
        first_response = client.post("/api/crawl/runs")
        first_job_id = first_response.json()["job_id"]
        claim_next(db_session)

        # Act
        second_response = client.post("/api/crawl/runs")

        # Assert
        assert second_response.status_code == 200
        assert second_response.json()["job_id"] == first_job_id

    def test_creates_a_new_job_when_the_previous_crawl_job_already_finished(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Arrange: 先行ジョブを completed にしてから再度起動する
        first_response = client.post("/api/crawl/runs")
        first_job_id = uuid.UUID(first_response.json()["job_id"])
        job = db_session.get(Job, first_job_id)
        assert job is not None
        claim_next(db_session)
        complete(db_session, job)

        # Act
        second_response = client.post("/api/crawl/runs")

        # Assert
        assert second_response.status_code == 201
        assert second_response.json()["job_id"] != str(first_job_id)
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.CRAWL_SOURCES.value)).all()
        assert len(jobs) == 2

    @pytest.mark.parametrize(
        "invalid_domain",
        [
            "not a domain",
            "169.254.169.254/latest",
            "",
            "-example.com",
            "example.com-",
        ],
    )
    def test_rejects_a_source_domain_with_an_invalid_format(
        self, client: TestClient, invalid_domain: str
    ) -> None:
        """LOW: ドメインとして妥当な文字種・構造でなければ 422 になること。"""
        # Act
        response = client.post("/api/crawl/runs", json={"source_domain": invalid_domain})

        # Assert
        assert response.status_code == 422
