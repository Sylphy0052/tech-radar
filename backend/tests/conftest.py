"""テスト共通のフィクスチャ。

DB を使うテストは専用のテストデータベースに対して実行する。開発用 DB を汚さないため。
テストデータベースは毎回作り直し、マイグレーションを適用してから使う。
これにより「マイグレーションが空 DB に適用できる」ことも同時に検証される。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from techradar.config import get_settings
from tests.db_process_isolation import (
    DATABASE_NAME_PREFIX,
    build_database_name,
    find_orphaned_database_names,
    find_own_worktree_legacy_database_names,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _test_database_name() -> str:
    """このプロセス専用のテスト用 DB 名を返す（Issue #23 / Issue #33）。

    テスト用 DB は毎回 DROP / CREATE するため、複数の worktree が同じ名前を使うと
    別の worktree のテスト実行中に DB を落としてしまい、無関係なテストが
    `AdminShutdown` などで落ちる（Issue #23）。さらに同じ worktree で pytest を
    複数プロセス同時実行しても互いの DB を破壊し合わないよう、プロセスの PID も
    名前に含める（Issue #33）。プロセス終了時には自分の DB を DROP するため
    （`_drop_own_test_database`）、DB が無尽蔵に増えることはない。異常終了時は
    セッション開始時の孤児 DB 掃除（`_cleanup_orphaned_test_databases`）が回収する。
    """
    return build_database_name(BACKEND_ROOT, os.getpid())


TEST_DATABASE_NAME = _test_database_name()

# テストで生成される `Settings()`（引数無し）は既定でジョブワーカーを起動しない。
# 実ワーカーが DB をポーリングし始めると、テストが不安定になりテスト用 DB の
# トランザクション（`db_session` のロールバック方式）とも干渉するため。
# `Settings(worker_enabled=True, ...)` のように明示指定したテストは
# 初期化引数が環境変数より優先されるため、この既定を上書きできる。
#
# `setdefault` ではなく代入で必ず上書きする。リポジトリルートの `run.sh` は
# `set -a; source .env; set +a` で `.env` の `WORKER_ENABLED=true` をシェルへ
# export するため、`./run.sh` を実行したのと同じシェルで `pytest` を叩くと
# シェル側の環境変数が既に設定済みになり、`setdefault` は無効化に失敗する。
os.environ["WORKER_ENABLED"] = "false"

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


def _admin_engine() -> Engine:
    """維持管理用の `postgres` データベースへ接続する AUTOCOMMIT エンジンを返す。

    CREATE / DROP DATABASE はトランザクション内で実行できないため AUTOCOMMIT にする。
    接続先の安全確認（`_assert_safe_to_drop`）はここで一括して行う。
    """
    admin_url = _database_url_for("postgres")
    _assert_safe_to_drop(admin_url)
    return create_engine(admin_url, isolation_level="AUTOCOMMIT")


def _terminate_connections(connection: Connection, database_name: str) -> None:
    """指定した DB への既存接続を強制切断する。接続が残っていると DROP が失敗するため。"""
    connection.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :name AND pid <> pg_backend_pid()"
        ),
        {"name": database_name},
    )


def _has_active_connections(connection: Connection, database_name: str) -> bool:
    """指定した DB への接続が（強制切断せずに）残っているかを返す。"""
    count = connection.execute(
        text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = :name AND pid <> pg_backend_pid()"
        ),
        {"name": database_name},
    ).scalar_one()
    return count > 0


def _recreate_test_database() -> None:
    """テスト用データベースを作り直す。

    自分専用の DB（プロセスごとに名前が分離されている）なので、既存接続は
    強制切断してよい。
    """
    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as connection:
            _terminate_connections(connection, TEST_DATABASE_NAME)
            connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}"'))
            connection.execute(text(f'CREATE DATABASE "{TEST_DATABASE_NAME}"'))
    finally:
        admin_engine.dispose()


def _drop_own_test_database() -> None:
    """セッション終了時に自分のテスト用 DB を片付ける（Issue #33）。

    DB 名をプロセスごとに分離した分、後始末をしないと DB が無尽蔵に増えてしまう。
    正常終了時はこの関数が回収し、異常終了で取りこぼした分は次回セッション開始時の
    `_cleanup_orphaned_test_databases` が回収する。
    """
    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as connection:
            _terminate_connections(connection, TEST_DATABASE_NAME)
            connection.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}"'))
    finally:
        admin_engine.dispose()


def _pid_is_alive(pid: int) -> bool:
    """指定 PID のプロセスが生存しているかを判定する。

    シグナル番号 0 はプロセスを実際には終了させず、存在確認だけを行う
    （`kill(2)` の慣用的な使い方）。`ProcessLookupError`（プロセスが存在しない）
    以外は、原因を問わずすべて「生存している」側に倒す（安全側に倒す）。

    権限不足で確認できない場合（別ユーザーのプロセス等）の `PermissionError` は
    `OSError` のサブクラスだが、`os.kill()` には巨大な PID（`sys.maxsize` 超）を
    渡すと `OverflowError` が飛ぶことがあり、これは `OSError` のサブクラスでは
    ない（Issue #33 self review）。DB 名の PID 部分の桁数には上限を設けている
    （`db_process_isolation.PID_DIGITS_MAX`）ため通常はここまで巨大な値は来ない
    はずだが、想定外の呼び出し経路に備えて `Exception` 全体を捕捉する。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def _existing_test_database_names(connection: Connection) -> list[str]:
    """このリポジトリのテスト用 DB 名を列挙する（他 worktree ・他プロセス分も含む）。"""
    result = connection.execute(
        text("SELECT datname FROM pg_database WHERE datname LIKE :pattern"),
        {"pattern": f"{DATABASE_NAME_PREFIX}%"},
    )
    return [row[0] for row in result]


def _cleanup_orphaned_test_databases() -> None:
    """異常終了した過去の pytest プロセスが残した孤児 DB を掃除する（Issue #33）。

    孤児かどうかの判定そのものは副作用の無い `find_orphaned_database_names` に
    任せ、ここでは実際の DROP と、その直前の「接続が残っていないか」の最終確認
    だけを行う。孤児と判定された DB でも接続が残っている場合は削除しない
    （他プロセスが使用中の可能性を否定できないため、消さない側に倒す）。
    """
    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as connection:
            existing_names = _existing_test_database_names(connection)
            orphans = find_orphaned_database_names(
                existing_names,
                backend_root=BACKEND_ROOT,
                own_pid=os.getpid(),
                is_pid_alive=_pid_is_alive,
            )
            for name in orphans:
                if _has_active_connections(connection, name):
                    continue
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        admin_engine.dispose()


def _cleanup_legacy_test_databases() -> None:
    """Issue #23 時代の PID 接尾辞なし旧形式 DB を掃除する（Issue #33 追加分）。

    旧形式（`techradar_test_<hash8>`）は PID を持たないため、
    `_cleanup_orphaned_test_databases` のような生存プロセス判定ができない。
    そのため「その DB への接続が残っていないこと」を唯一の生存シグナルとして扱い、
    `_has_active_connections` の確認を必須にする（1 件でも接続が残っていれば
    そのDBはスキップし、消さない側に倒す）。

    掃除対象は自分の worktree ハッシュを持つ DB に限る
    （`find_own_worktree_legacy_database_names` が絞り込み済み）。別 worktree の
    ハッシュを持つ旧形式 DB は、そちらの worktree でまだ旧コードの pytest が
    走りうるため絶対に触らない。
    """
    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as connection:
            existing_names = _existing_test_database_names(connection)
            candidates = find_own_worktree_legacy_database_names(
                existing_names,
                backend_root=BACKEND_ROOT,
            )
            for name in candidates:
                if _has_active_connections(connection, name):
                    continue
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """マイグレーション適用済みのテスト用 DB へ接続するエンジン。"""
    _cleanup_orphaned_test_databases()
    _cleanup_legacy_test_databases()
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
        _drop_own_test_database()


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
