"""`techradar.jobs.handlers._shared` の共通処理を検証する。

`run_job_in_thread` は本番では `JobWorker` を経由して呼ばれ、内部で
`techradar.db.session.get_session_factory()` を直接使う（`JobWorker` と違い
注入用の引数を持たない）。実際にスレッド越しにコミットされることを検証する
には、この関数自体をテスト用 DB を指すファクトリへ差し替える必要がある。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import ArticleRegistration
from techradar.fetcher.url import normalize_url
from techradar.jobs.handlers import _shared
from techradar.jobs.handlers._shared import (
    is_final_attempt,
    load_registration,
    record_registration_failure,
    run_job_in_thread,
)
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.registry import JobContext


@pytest.fixture
def handler_session_factory(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[sessionmaker[Session]]:
    """`_shared.get_session_factory` をテスト用 DB を指すファクトリへ差し替える。"""
    engine = create_engine(migrated_engine.url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(_shared, "get_session_factory", lambda: factory)
    try:
        yield factory
    finally:
        with Session(bind=migrated_engine) as cleanup_session:
            cleanup_session.execute(delete(ArticleRegistration))
            cleanup_session.commit()
        engine.dispose()


def make_registration_row(session_factory: sessionmaker[Session]) -> uuid.UUID:
    with session_factory() as session:
        registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/a",
            normalized_url=normalize_url("https://example.com/a"),
            status=JobStatus.PENDING.value,
        )
        session.add(registration)
        session.commit()
        return registration.id


def make_context(registration_id: uuid.UUID, *, attempts: int = 0) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.FETCH_ARTICLE,
        payload={"registration_id": str(registration_id)},
        attempts=attempts,
    )


class TestIsFinalAttempt:
    def test_is_false_when_retries_remain(self) -> None:
        # Arrange
        settings = Settings(_env_file=None, job_max_attempts=3)
        context = make_context(uuid.uuid4(), attempts=0)

        # Act / Assert
        assert is_final_attempt(context, settings) is False

    def test_is_true_once_the_next_failure_would_exhaust_the_budget(self) -> None:
        # Arrange
        settings = Settings(_env_file=None, job_max_attempts=3)
        context = make_context(uuid.uuid4(), attempts=2)

        # Act / Assert
        assert is_final_attempt(context, settings) is True

    def test_is_true_on_the_first_attempt_when_max_attempts_is_one(self) -> None:
        # Arrange
        settings = Settings(_env_file=None, job_max_attempts=1)
        context = make_context(uuid.uuid4(), attempts=0)

        # Act / Assert
        assert is_final_attempt(context, settings) is True


class TestRunJobInThread:
    async def test_commits_the_operations_writes_on_success(
        self, handler_session_factory: sessionmaker[Session]
    ) -> None:
        # Arrange
        registration_id = make_registration_row(handler_session_factory)
        settings = Settings(_env_file=None)
        context = make_context(registration_id)

        def _operation(session: Session, ctx: JobContext, _config: Settings) -> None:
            registration = load_registration(session, uuid.UUID(ctx.payload["registration_id"]))
            assert registration is not None
            registration.error_reason = "committed-by-thread"

        # Act
        await run_job_in_thread(context, settings, _operation)

        # Assert — 別セッションから読み直しても見える（実際に commit されている）
        with handler_session_factory() as verify_session:
            reloaded = verify_session.get(ArticleRegistration, registration_id)
            assert reloaded is not None
            assert reloaded.error_reason == "committed-by-thread"

    async def test_commits_the_failure_record_before_reraising_the_original_exception(
        self, handler_session_factory: sessionmaker[Session]
    ) -> None:
        # Arrange
        registration_id = make_registration_row(handler_session_factory)
        settings = Settings(_env_file=None, job_max_attempts=1)
        context = make_context(registration_id, attempts=0)

        def _operation(session: Session, ctx: JobContext, config: Settings) -> None:
            registration = load_registration(session, uuid.UUID(ctx.payload["registration_id"]))
            assert registration is not None
            record_registration_failure(
                session,
                registration,
                RegistrationErrorReason.FETCH_FAILED,
                context=ctx,
                settings=config,
            )
            message = "boom"
            raise RuntimeError(message)

        # Act
        with pytest.raises(RuntimeError, match="boom"):
            await run_job_in_thread(context, settings, _operation)

        # Assert — 例外はそのまま伝播しつつ、失敗の記録は rollback されず残る
        with handler_session_factory() as verify_session:
            reloaded = verify_session.get(ArticleRegistration, registration_id)
            assert reloaded is not None
            assert reloaded.error_reason == RegistrationErrorReason.FETCH_FAILED.value
            assert reloaded.status == JobStatus.FAILED.value
