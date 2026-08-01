"""テスト共通のフィクスチャ。

DB を使うテストは専用のテストデータベースに対して実行する。開発用 DB を汚さないため。
テストデータベースは毎回作り直し、マイグレーションを適用してから使う。
これにより「マイグレーションが空 DB に適用できる」ことも同時に検証される。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from techradar.config import get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_NAME = "techradar_test"

# テスト用 DB の作り直しは破壊的なため、接続先をローカルと CI のサービスコンテナに限定する。
# "postgres" は GitLab CI の service alias。
ALLOWED_TEST_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})


def _test_database_url() -> str:
    """開発用 DB と同じサーバー上のテスト用 DB を指す URL を返す。"""
    return _database_url_for(TEST_DATABASE_NAME)


def _database_url_for(database: str) -> str:
    """設定の接続情報を流用し、データベース名だけ差し替えた URL を返す。

    SQLAlchemy の `URL.__str__` はパスワードを `***` にマスクするため、
    接続に使う文字列は `render_as_string(hide_password=False)` で組み立てる。
    """
    base = make_url(str(get_settings().database_url))
    return base.set(database=database).render_as_string(hide_password=False)


def _assert_safe_to_drop(url: str) -> None:
    """破壊的 DDL を実行してよい接続先かを検証する。

    `DROP DATABASE` を無条件に実行すると、`DATABASE_URL` が共有 DB や
    ステージングを指していた場合に、同名の DB を巻き添えで破壊しうる。
    ローカルまたは CI のサービスコンテナ以外へは接続させない。
    """
    host = (make_url(url).host or "").lower()
    if host not in ALLOWED_TEST_DB_HOSTS:
        message = (
            f"テスト用 DB の再作成は {sorted(ALLOWED_TEST_DB_HOSTS)} に対してのみ許可しています "
            f"(接続先ホスト: {host or '(未指定)'})。DATABASE_URL を確認してください。"
        )
        raise RuntimeError(message)


def _recreate_test_database() -> None:
    """テスト用データベースを作り直す。

    接続には維持管理用の `postgres` データベースを使う。CREATE / DROP DATABASE は
    トランザクション内で実行できないため AUTOCOMMIT にする。
    """
    admin_url = _database_url_for("postgres")
    _assert_safe_to_drop(admin_url)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as connection:
            # 既存接続が残っていると DROP が失敗するため強制切断する。
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": TEST_DATABASE_NAME},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}"'))
            connection.execute(text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """マイグレーション適用済みのテスト用 DB へ接続するエンジン。"""
    _recreate_test_database()

    alembic_config = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", _test_database_url())
    command.upgrade(alembic_config, "head")

    engine = create_engine(_test_database_url())
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    """テストごとにロールバックされるセッション。

    外側のトランザクションを張り、テスト終了時にロールバックすることで
    テスト間のデータ汚染を防ぐ。
    """
    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        # 制約違反を検証するテストでは、その時点で外側のトランザクションが
        # 既に解除されていることがある。
        if transaction.is_active:
            transaction.rollback()
        connection.close()
