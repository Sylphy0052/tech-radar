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
from sqlalchemy import Engine, create_engine, delete, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
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
    record_registration_failure_safely,
    run_job_in_thread,
)
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.registry import JobContext

# NOT NULL 違反の SQLSTATE。伝播した例外が元の原因由来かの確認に使う。
NOT_NULL_VIOLATION_SQLSTATE = "23502"


class _ConnectionClosed(Exception):
    """接続断を模した DBAPI 例外。"""


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


class TestRecordFailureAfterRollback:
    def test_records_the_reason_even_when_the_transaction_is_already_broken(
        self, handler_session_factory: sessionmaker[Session]
    ) -> None:
        """DB 由来の例外で中断したトランザクションでも失敗理由を残せること。

        中断状態のまま flush すると `InFailedSqlTransaction` で書き込み自体が
        失敗し、登録が実行中のまま取り残される。
        """
        # Arrange
        registration_id = make_registration_row(handler_session_factory)
        settings = Settings(_env_file=None, job_max_attempts=1)
        context = make_context(registration_id, attempts=0)

        with handler_session_factory() as session:
            # NOT NULL 違反でトランザクションを中断状態にする。
            with pytest.raises(DBAPIError):
                session.execute(
                    text(
                        "INSERT INTO jobs (id, type, status) "
                        "VALUES (gen_random_uuid(), NULL, 'pending')"
                    )
                )

            # Act
            record_registration_failure_safely(
                session,
                registration_id,
                RegistrationErrorReason.UNEXPECTED_FAILURE,
                context=context,
                settings=settings,
            )
            session.commit()

        # Assert — 別セッションから読み直しても記録が残っている
        with handler_session_factory() as verify_session:
            reloaded = verify_session.get(ArticleRegistration, registration_id)
            assert reloaded is not None
            assert reloaded.error_reason == RegistrationErrorReason.UNEXPECTED_FAILURE.value
            assert reloaded.status == JobStatus.FAILED.value

    def test_returns_without_raising_when_the_registration_is_gone(
        self, handler_session_factory: sessionmaker[Session]
    ) -> None:
        # Arrange
        settings = Settings(_env_file=None)
        context = make_context(uuid.uuid4())

        # Act / Assert — 記録対象が無くても例外にしない（元の例外を隠さないため）
        with handler_session_factory() as session:
            record_registration_failure_safely(
                session,
                uuid.uuid4(),
                RegistrationErrorReason.UNEXPECTED_FAILURE,
                context=context,
                settings=settings,
            )

    def test_returns_without_raising_when_even_the_rollback_fails(
        self, handler_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """巻き戻し自体が失敗しても例外を外へ出さないこと。

        接続が切れている場合は rollback すら通らない。ここで例外を送出すると、
        呼び出し元が元の障害を再送出する前に別の例外へすり替わってしまう。
        """
        # Arrange
        registration_id = make_registration_row(handler_session_factory)
        settings = Settings(_env_file=None, job_max_attempts=1)
        context = make_context(registration_id, attempts=0)

        with handler_session_factory() as session:
            with pytest.raises(DBAPIError):
                session.execute(
                    text(
                        "INSERT INTO jobs (id, type, status) "
                        "VALUES (gen_random_uuid(), NULL, 'pending')"
                    )
                )

            def _raise_on_rollback() -> None:
                raise OperationalError("ROLLBACK", {}, orig=_ConnectionClosed())

            monkeypatch.setattr(session, "rollback", _raise_on_rollback)

            # Act / Assert — 例外を送出せずに戻る
            record_registration_failure_safely(
                session,
                registration_id,
                RegistrationErrorReason.UNEXPECTED_FAILURE,
                context=context,
                settings=settings,
            )


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

    async def test_records_a_failure_caused_by_a_database_error(
        self, handler_session_factory: sessionmaker[Session]
    ) -> None:
        """DB 由来の例外で処理が落ちても、失敗理由が記録され元の例外が伝播すること。

        ハンドラの捕捉範囲には `enqueue` などの DB 書き込みも含まれる。中断した
        トランザクションのまま記録しようとすると、記録が残らないうえ呼び出し元へ
        伝わる例外まで別のものへすり替わる。
        """
        # Arrange
        registration_id = make_registration_row(handler_session_factory)
        settings = Settings(_env_file=None, job_max_attempts=1)
        context = make_context(registration_id, attempts=0)

        def _operation(session: Session, ctx: JobContext, config: Settings) -> None:
            registration_uuid = uuid.UUID(ctx.payload["registration_id"])
            try:
                # NOT NULL 違反。ハンドラ内の DB 書き込みが失敗する状況を模す。
                session.execute(
                    text(
                        "INSERT INTO jobs (id, type, status) "
                        "VALUES (gen_random_uuid(), NULL, 'pending')"
                    )
                )
            except Exception as exc:
                record_registration_failure_safely(
                    session,
                    registration_uuid,
                    RegistrationErrorReason.UNEXPECTED_FAILURE,
                    context=ctx,
                    settings=config,
                )
                raise exc from None

        # Act
        with pytest.raises(DBAPIError) as exc_info:
            await run_job_in_thread(context, settings, _operation)

        # Assert — 伝播したのは元の NOT NULL 違反そのもの（記録処理の二次エラーではない）
        assert isinstance(exc_info.value, IntegrityError)
        assert getattr(exc_info.value.orig, "sqlstate", None) == NOT_NULL_VIOLATION_SQLSTATE

        with handler_session_factory() as verify_session:
            reloaded = verify_session.get(ArticleRegistration, registration_id)
            assert reloaded is not None
            assert reloaded.error_reason == RegistrationErrorReason.UNEXPECTED_FAILURE.value
            assert reloaded.status == JobStatus.FAILED.value

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
