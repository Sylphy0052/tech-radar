"""セッション管理とマイグレーションの可逆性、テスト基盤の安全装置を検証する。"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from techradar.db import Job, session_scope
from techradar.db.enums import JobType
from techradar.db.session import get_engine, get_session_factory
from tests.conftest import BACKEND_ROOT, _assert_safe_to_drop, _test_database_url


@pytest.fixture
def scoped_session_factory(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """`session_scope` をテスト用 DB へ向ける。"""
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    monkeypatch.setattr("techradar.db.session.get_session_factory", lambda: factory)
    return factory


class TestSessionScope:
    def test_commits_on_success(self, scoped_session_factory, migrated_engine: Engine):
        # Arrange
        job_id = uuid.uuid4()

        # Act
        with session_scope() as session:
            session.add(Job(id=job_id, type=JobType.CRAWL_SOURCES, payload={}))

        # Assert — ブロックを抜けた時点で確定していること
        with migrated_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": job_id}
            ).scalar_one()
        assert stored == 1

        # Cleanup
        with migrated_engine.begin() as connection:
            connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})

    def test_rolls_back_and_reraises_on_failure(
        self, scoped_session_factory, migrated_engine: Engine
    ):
        # Arrange
        job_id = uuid.uuid4()

        # Act / Assert — 例外は握りつぶさず呼び出し側へ伝える
        with pytest.raises(RuntimeError, match="boom"):
            with session_scope() as session:
                session.add(Job(id=job_id, type=JobType.CRAWL_SOURCES, payload={}))
                session.flush()
                raise RuntimeError("boom")

        # Assert — 途中の書き込みが残っていないこと
        with migrated_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": job_id}
            ).scalar_one()
        assert stored == 0


class TestEngineCache:
    def test_reuses_a_single_engine(self, monkeypatch: pytest.MonkeyPatch):
        # Arrange — 実接続を張らずにキャッシュの振る舞いだけを見る
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        monkeypatch.setenv("DATABASE_URL", _test_database_url())

        # Act
        first = get_engine()
        second = get_engine()

        # Assert
        assert first is second

        # Cleanup
        first.dispose()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


class TestMigrationReversibility:
    def test_downgrades_to_base_and_upgrades_again(self):
        # Arrange — 他テストの共有 DB を壊さないよう専用 DB を使う
        database_name = f"techradar_reversibility_{uuid.uuid4().hex[:8]}"
        admin_engine = create_engine(
            _database_url_for_test("postgres"), isolation_level="AUTOCOMMIT"
        )
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        target_url = _database_url_for_test(database_name)
        alembic_config = Config(str(BACKEND_ROOT / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        alembic_config.set_main_option("sqlalchemy.url", target_url)

        try:
            # Act / Assert — upgrade でテーブルが作られる
            command.upgrade(alembic_config, "head")
            engine = create_engine(target_url)
            try:
                assert "articles" in inspect(engine).get_table_names()

                # Act / Assert — downgrade で alembic_version 以外が消える
                command.downgrade(alembic_config, "base")
                remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
                assert remaining == set()

                # Act / Assert — 再度 upgrade できる
                command.upgrade(alembic_config, "head")
                assert "articles" in inspect(engine).get_table_names()
            finally:
                engine.dispose()
        finally:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
            admin_engine.dispose()


class TestDropGuard:
    def test_allows_local_hosts(self):
        # Arrange / Act / Assert
        _assert_safe_to_drop("postgresql+psycopg://u:p@localhost:5432/postgres")
        _assert_safe_to_drop("postgresql+psycopg://u:p@postgres:5432/postgres")

    def test_rejects_remote_hosts(self):
        # Arrange / Act / Assert — 共有 DB を誤って壊さないための安全装置
        with pytest.raises(RuntimeError, match="テスト用 DB の再作成"):
            _assert_safe_to_drop("postgresql+psycopg://u:p@db.internal.example.com:5432/postgres")


def _database_url_for_test(database: str) -> str:
    """テスト用 DB と同じサーバーの、指定データベースを指す URL を返す。"""
    from sqlalchemy.engine import make_url

    return (
        make_url(_test_database_url()).set(database=database).render_as_string(hide_password=False)
    )
