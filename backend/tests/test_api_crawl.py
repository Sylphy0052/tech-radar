"""巡回ジョブの起動 API を検証する（`PROJECT_SPEC.md` §20、Issue #8）。"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api import crawl
from techradar.api.crawl import CrawlRunResponse, create_crawl_run
from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import ACTIVE_CRAWL_JOB_INDEX_PREDICATE, Job
from techradar.jobs.queue import claim_next, complete, enqueue, ownership_token
from techradar.jobs.status import running_status_for
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

    `db_session` フィクスチャは1本の接続を共有し外側トランザクションでロールバックする
    作りのため、2つの接続が同時に INSERT して一意制約で競合する状況は再現できない。
    ここでは接続ごとに独立したトランザクションを張り、テストがコミットした
    `crawl_sources` ジョブの後始末まで責任を持つ。
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
            cleanup_session.execute(delete(Job).where(Job.type == JobType.CRAWL_SOURCES.value))
            cleanup_session.commit()


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

    def test_commits_the_job_before_responding(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """受入基準: 応答を受け取った時点で、別接続からもジョブを追跡できること。

        リクエスト単位のセッションは FastAPI の依存の後処理でコミットされるが、
        後処理が走るのはレスポンス送信より後になる。UI は応答直後に
        `GET /api/jobs/{job_id}` を叩くため、コミットを応答前に済ませておかないと
        起動したばかりのジョブが 404 になる。
        """
        # Arrange
        api_session = independent_sessions()
        observer = independent_sessions()
        app = create_app(Settings(_env_file=None))
        app.dependency_overrides[get_session] = lambda: api_session

        # Act
        with TestClient(app) as test_client:
            response = test_client.post("/api/crawl/runs")

        # Assert — 応答時点で別接続から見えること（コミット済みであること）
        assert response.status_code == 201
        job_id = uuid.UUID(response.json()["job_id"])
        assert observer.get(Job, job_id) is not None

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
        complete(db_session, job, claimed_at=ownership_token(job))

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


class TestCreateCrawlRunUnderConcurrency:
    """重複起動防止が並行リクエストでも破られないこと（Issue #26）。

    `_find_active_crawl_job` による事前確認だけでは、確認と INSERT の間に
    別リクエストが割り込む TOCTOU レースを防げない。DB の部分ユニークインデックスが
    最終的な防衛線として効いていることを、独立した接続を使って検証する。
    """

    def test_the_database_rejects_a_second_active_crawl_job(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """受入基準: アクティブな crawl_sources ジョブは DB 制約で1件に制限される。"""
        # Arrange: 1件目をコミットして確定させる
        session_a = independent_sessions()
        enqueue(session_a, JobType.CRAWL_SOURCES, {})
        session_a.commit()

        # Act / Assert: 別接続から2件目を積むと flush の時点で一意制約に弾かれる
        session_b = independent_sessions()
        with pytest.raises(IntegrityError):
            enqueue(session_b, JobType.CRAWL_SOURCES, {})

    def test_the_database_rejects_a_second_job_while_the_first_one_is_running(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """インデックスの述語が pending だけでなく実行中 status も覆っていること。

        アプリ層の事前確認だけを通るテストでは、述語から searching が抜け落ちても
        気付けない。DB 制約そのものが実行中ジョブを守っていることを確かめる。
        """
        # Arrange: 1件目を claim して searching へ遷移させ、コミットする
        session_a = independent_sessions()
        enqueue(session_a, JobType.CRAWL_SOURCES, {})
        claimed = claim_next(session_a)
        assert claimed is not None
        assert claimed.status == running_status_for(JobType.CRAWL_SOURCES).value
        session_a.commit()

        # Act / Assert
        session_b = independent_sessions()
        with pytest.raises(IntegrityError):
            enqueue(session_b, JobType.CRAWL_SOURCES, {})

    def test_the_index_predicate_covers_exactly_the_statuses_the_api_treats_as_active(
        self,
    ) -> None:
        """インデックスの述語とアプリ側の「アクティブ」判定がずれていないこと。

        DDL の述語は文字列のため型チェックが効かず、`jobs/status.py` の写像を
        変えても DB 側は追随しない。両者の乖離をここで検出する。
        """
        # Arrange
        expected_statuses = {
            JobStatus.PENDING.value,
            running_status_for(JobType.CRAWL_SOURCES).value,
        }

        # Assert: アプリ側の集合と、DDL 述語に現れる status が一致する
        assert crawl._ACTIVE_CRAWL_JOB_STATUSES == expected_statuses
        for status_value in expected_statuses:
            assert f"'{status_value}'" in ACTIVE_CRAWL_JOB_INDEX_PREDICATE
        assert f"type = '{JobType.CRAWL_SOURCES.value}'" in ACTIVE_CRAWL_JOB_INDEX_PREDICATE

    def test_a_finished_crawl_job_does_not_block_the_next_one(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """完了済みジョブは制約の対象外（インデックスの述語が status を絞っていること）。"""
        # Arrange: 1件目を completed まで進めてコミットする
        session = independent_sessions()
        job = enqueue(session, JobType.CRAWL_SOURCES, {})
        claim_next(session)
        complete(session, job, claimed_at=ownership_token(job))
        session.commit()

        # Act: 2件目を積む
        enqueue(session, JobType.CRAWL_SOURCES, {})
        session.commit()

        # Assert: 制約に阻まれず2件目が作られる
        jobs = session.scalars(select(Job).where(Job.type == JobType.CRAWL_SOURCES.value)).all()
        assert len(jobs) == 2

    def test_only_one_job_is_created_when_two_requests_race(
        self, independent_sessions: Callable[[], Session]
    ) -> None:
        """受入基準: 同時に叩かれても作られるジョブは1件で、敗れた側も job_id を受け取る。

        2本の独立した接続から `create_crawl_run` をほぼ同時に実行する。両者が
        「アクティブなジョブなし」と判定した場合、後から flush した側は一意制約で
        待たされ、相手のコミット後に `IntegrityError` を受け取る。その経路でも
        エラーを返さず、既存ジョブを 200 OK で返すことを確認する。
        """
        # Arrange
        session_a = independent_sessions()
        session_b = independent_sessions()
        start = threading.Barrier(2)

        def call(session: Session) -> tuple[CrawlRunResponse, int]:
            # ルーターのデコレータが与える既定値 (201 Created) を模す。関数を直接
            # 呼ぶとその機構は働かないため、既存ジョブ返却時の 200 への上書きだけを
            # ここで観測する。
            response = Response(status_code=201)
            start.wait()
            body = create_crawl_run(session, response, None)
            session.commit()
            return body, response.status_code

        # Act
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result()
                for future in [executor.submit(call, s) for s in (session_a, session_b)]
            ]

        # Assert: 作られたジョブは1件だけで、両者が同じ job_id を受け取っている
        with Session(bind=session_a.get_bind()) as verify_session:
            jobs = verify_session.scalars(
                select(Job).where(Job.type == JobType.CRAWL_SOURCES.value)
            ).all()
        assert len(jobs) == 1

        returned_job_ids = {body.job_id for body, _ in results}
        assert returned_job_ids == {jobs[0].id}

        # 片方は新規作成 (201)、もう片方は既存ジョブの返却 (200)。
        # 先行した側が完全に終わってから後発が SELECT した場合は両方 200 にはならず、
        # 必ず 201 が1つだけ立つ。
        status_codes = sorted(status_code for _, status_code in results)
        assert status_codes == [200, 201]

    def test_returns_the_existing_job_when_the_unique_index_rejects_the_insert(
        self, independent_sessions: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 事前確認をすり抜けた側も、エラーではなく既存 job_id を 200 で受け取る。

        スレッドを使うテストは、実行のたびに「事前確認が競合を見逃す」経路を
        通るとは限らない。ここでは事前確認だけを1回空振りさせ、一意制約違反を
        受け取る経路を決定論的に通す。
        """
        # Arrange: アクティブなジョブを1件コミットして確定させる
        setup_session = independent_sessions()
        existing_job_id = enqueue(setup_session, JobType.CRAWL_SOURCES, {}).id
        setup_session.commit()

        real_find_active_crawl_job = crawl._find_active_crawl_job
        remaining_misses = 1

        def find_active_crawl_job_missing_once(session: Session) -> Job | None:
            nonlocal remaining_misses
            if remaining_misses:
                remaining_misses -= 1
                return None
            return real_find_active_crawl_job(session)

        monkeypatch.setattr(crawl, "_find_active_crawl_job", find_active_crawl_job_missing_once)

        # Act
        session = independent_sessions()
        response = Response(status_code=201)
        body = create_crawl_run(session, response, None)

        # Assert
        assert response.status_code == 200
        assert body.job_id == existing_job_id

    def test_reraises_when_the_conflicting_job_disappears_before_it_can_be_returned(
        self, independent_sessions: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """競合相手が引き直しまでに終了していた場合は、重複として握り潰さず送出する。"""
        # Arrange
        setup_session = independent_sessions()
        enqueue(setup_session, JobType.CRAWL_SOURCES, {})
        setup_session.commit()
        monkeypatch.setattr(crawl, "_find_active_crawl_job", lambda session: None)

        # Act / Assert
        session = independent_sessions()
        with pytest.raises(IntegrityError):
            create_crawl_run(session, Response(status_code=201), None)

    def test_does_not_swallow_an_integrity_error_that_is_not_a_unique_violation(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一意制約違反以外の IntegrityError は重複として握り潰さず送出すること。

        すべての IntegrityError を「相手が先に積んだ」と解釈すると、外部キー違反や
        将来追加される CHECK 制約の違反まで 200 OK に化けてしまう。
        """
        # Arrange: 一意制約違反ではない IntegrityError (外部キー違反) を起こさせる
        foreign_key_violation = IntegrityError(
            "INSERT INTO jobs ...", {}, ForeignKeyViolation("insert or update violates foreign key")
        )

        def raise_foreign_key_violation(*args: object, **kwargs: object) -> Job:
            raise foreign_key_violation

        monkeypatch.setattr(crawl, "enqueue", raise_foreign_key_violation)

        # Act / Assert
        with pytest.raises(IntegrityError) as excinfo:
            create_crawl_run(db_session, Response(status_code=201), None)
        assert excinfo.value is foreign_key_violation
