"""`tests/conftest.py` の実DBに触れる掃除関数の統合テスト（Issue #33 self review 対応）。

`_has_active_connections` / `_cleanup_orphaned_test_databases` /
`_cleanup_legacy_test_databases` は `DROP DATABASE` を含む破壊的操作でありながら
自動テストが無く、手動実行ログでしか検証されていなかった。「接続が残っている DB は
スキップする」という安全弁の分岐が万一反転しても、通常の pytest 実行では検出できない。

このテストは実 PostgreSQL に対して動作するため、`tests.conftest.BACKEND_ROOT` を
`monkeypatch` で一時的にダミーの worktree パスへ差し替え、実際の worktree ハッシュとは
無関係な名前空間で DB を作る。こうすることで、

- 別 worktree（別ハッシュ）が使っている実テスト用 DB を巻き込まない
- 本番の掃除ロジック（他プロセスがこのリポジトリの本来の worktree ハッシュで実行する
  `_cleanup_orphaned_test_databases` 等）が、このテストが一時的に作った DB を
  実行中に拾って消してしまう（自己干渉）ことを避ける

という2点を両立する。作った DB はテスト自身が必ず後始末する（`cleanup_database_names`
フィクスチャが `finally` 相当のティアダウンで DROP する。失敗時も残さない）。

ダミーのパスは固定値ではなく、実行中の worktree と pytest プロセスから派生させる
（Issue #59）。固定にするとどこから実行しても同じ DB 名になり、複数の worktree や
プロセスで同時に pytest を回したときに `DuplicateDatabase` で落ちる。
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from tests import conftest as conftest_module
from tests.db_process_isolation import DATABASE_NAME_PREFIX, build_database_name, worktree_hash


def _fake_backend_root(base: Path, label: str, pid: int) -> Path:
    """このテスト専用のダミー backend_root を組み立てる（Issue #59）。

    実 backend_root の兄弟にあたる、実在しないパスを返す。ハッシュは実 worktree の
    ものとは別値になるため、掃除ロジックが見る「自分のハッシュ」を巻き込まない。

    派生元に実 backend_root と実行中プロセスの PID を使うのは、ダミー DB 名が
    worktree とプロセスをまたいで衝突しないようにするため。固定パスにすると
    どの worktree のどのプロセスから実行しても同じ DB 名になり、複数の pytest が
    同時に走ったときに `DuplicateDatabase` で落ちる。ダミー PID（`_DEAD_PID`）は
    固定のままでよい。名前空間はハッシュ側で分かれる。
    """
    return base.parent / f"{base.name}-test-fixture-{label}-{pid}"


# このテスト専用のダミー backend_root。実 worktree・実プロセスから派生させるため、
# 別 worktree や別プロセスの pytest が同時に走っても DB 名を取り合わない。
_FAKE_BACKEND_ROOT = _fake_backend_root(conftest_module.BACKEND_ROOT, "a", os.getpid())
_OTHER_FAKE_BACKEND_ROOT = _fake_backend_root(conftest_module.BACKEND_ROOT, "b", os.getpid())

# OS 上に実在しえない極端に大きい PID（32bit 符号付き pid_t の上限付近）。
# 実プロセスをフォーク/待機させずに「確実に死んでいる PID」を得るために使う。
_DEAD_PID = 2147483647
_ANOTHER_DEAD_PID = 2147483646


def _database_exists(connection: Connection, database_name: str) -> bool:
    """指定した名前の DB が存在するかを返す。"""
    result = connection.execute(
        text("SELECT 1 FROM pg_database WHERE datname = :name"),
        {"name": database_name},
    )
    return result.first() is not None


def _wait_until_no_active_connections(
    admin_engine: Engine, database_name: str, *, timeout_seconds: float = 2.0
) -> bool:
    """`database_name` への接続が無くなるまで短時間ポーリングする。

    クライアント側の `connection.close()` は即座に返るが、PostgreSQL 側の
    バックエンドプロセスが `pg_stat_activity` から消えるまでに数十ms程度の
    ラグが生じることがある（負荷が高いとき、フルスイート実行中に実測）。
    1回きりの確認だとこのラグをそのままテストの flaky として顕在化させて
    しまうため、短時間だけポーリングして安定させる。
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        with admin_engine.connect() as connection:
            if not conftest_module._has_active_connections(connection, database_name):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _wait_until_has_active_connection(
    admin_engine: Engine, database_name: str, *, timeout_seconds: float = 2.0
) -> bool:
    """`database_name` への接続が `pg_stat_activity` に現れるまで短時間ポーリングする。

    クライアント側で `connection.connect()` が返っても、PostgreSQL 側の
    バックエンドプロセスが `pg_stat_activity` に載るまでに数十ms程度のラグが
    生じることがある。「接続が残っている DB は消さない」を検証するテストで
    このラグを踏むと、掃除関数がまだ接続を検知できていないだけなのに
    誤って削除されたように見えてしまう（安全弁の反転を誤検出する）ため、
    掃除関数を呼ぶ前に接続が確実に見えている状態まで待つ。
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        with admin_engine.connect() as connection:
            if conftest_module._has_active_connections(connection, database_name):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


@pytest.fixture
def admin_engine() -> Iterator[Engine]:
    """維持管理用 DB への AUTOCOMMIT エンジン。テスト終了時に必ず破棄する。"""
    engine = conftest_module._admin_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def cleanup_database_names(admin_engine: Engine) -> Iterator[list[str]]:
    """テストが作った一時 DB 名を集めるリスト。

    テスト終了時（失敗時も含む）に、集めた名前をすべて強制切断のうえ DROP する。
    生成した DB を確実に後始末し、他 worktree のテスト用 DB を巻き込まないための仕組み。
    """
    names: list[str] = []
    try:
        yield names
    finally:
        with admin_engine.connect() as connection:
            for name in names:
                conftest_module._terminate_connections(connection, name)
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


class TestHasActiveConnections:
    """`_has_active_connections` の実DBに対する振る舞い。"""

    def test_true_while_connected_false_after_disconnect(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        # Arrange
        db_name = build_database_name(_FAKE_BACKEND_ROOT, os.getpid())
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        # Assert — 接続前は偽
        with admin_engine.connect() as connection:
            assert conftest_module._has_active_connections(connection, db_name) is False

        # Act — 対象DBへ実際に接続を張る
        target_engine = create_engine(conftest_module._database_url_for(db_name))
        target_connection = target_engine.connect()
        try:
            # Assert — 接続中は真
            with admin_engine.connect() as connection:
                assert conftest_module._has_active_connections(connection, db_name) is True
        finally:
            target_connection.close()
            target_engine.dispose()

        # Assert — 切断後は偽に戻る
        # （バックエンドプロセスの後始末が非同期のため短時間ポーリングする）
        assert _wait_until_no_active_connections(admin_engine, db_name) is True


class TestCleanupOrphanedTestDatabases:
    """`_cleanup_orphaned_test_databases` の実DBに対する振る舞い。"""

    def test_drops_dead_pid_keeps_alive_pid_own_pid_and_other_worktree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        # Arrange — 掃除対象の worktree をダミーへ差し替える
        monkeypatch.setattr(conftest_module, "BACKEND_ROOT", _FAKE_BACKEND_ROOT)

        own_pid = os.getpid()
        # 生存中プロセスとして、固定引数のみを渡す子プロセスを立てる
        # （信頼できる固定コマンドのため shell 経由ではなく、PATH検索も許容する）。
        alive_process = subprocess.Popen(["sleep", "5"])  # noqa: S607
        try:
            alive_pid = alive_process.pid

            dead_pid_name = build_database_name(_FAKE_BACKEND_ROOT, _DEAD_PID)
            alive_pid_name = build_database_name(_FAKE_BACKEND_ROOT, alive_pid)
            own_pid_name = build_database_name(_FAKE_BACKEND_ROOT, own_pid)
            other_worktree_name = build_database_name(_OTHER_FAKE_BACKEND_ROOT, _DEAD_PID)
            names = [dead_pid_name, alive_pid_name, own_pid_name, other_worktree_name]

            with admin_engine.connect() as connection:
                for name in names:
                    connection.execute(text(f'CREATE DATABASE "{name}"'))
            cleanup_database_names.extend(names)

            # Act
            conftest_module._cleanup_orphaned_test_databases()

            # Assert
            with admin_engine.connect() as connection:
                assert _database_exists(connection, dead_pid_name) is False
                assert _database_exists(connection, alive_pid_name) is True
                assert _database_exists(connection, own_pid_name) is True
                assert _database_exists(connection, other_worktree_name) is True
        finally:
            alive_process.terminate()
            alive_process.wait(timeout=5)

    def test_skips_dead_pid_database_with_active_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        # Arrange
        monkeypatch.setattr(conftest_module, "BACKEND_ROOT", _FAKE_BACKEND_ROOT)
        name = build_database_name(_FAKE_BACKEND_ROOT, _ANOTHER_DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        cleanup_database_names.append(name)

        target_engine = create_engine(conftest_module._database_url_for(name))
        target_connection = target_engine.connect()
        try:
            assert _wait_until_has_active_connection(admin_engine, name) is True

            # Act — 孤児判定（PID死亡・自分のworktree）は通るが、接続が残っている
            conftest_module._cleanup_orphaned_test_databases()

            # Assert — 消さない側に倒れる
            with admin_engine.connect() as connection:
                assert _database_exists(connection, name) is True
        finally:
            target_connection.close()
            target_engine.dispose()


class TestCleanupLegacyTestDatabases:
    """`_cleanup_legacy_test_databases` の実DBに対する振る舞い。"""

    def test_drops_own_worktree_keeps_other_worktree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        # Arrange
        monkeypatch.setattr(conftest_module, "BACKEND_ROOT", _FAKE_BACKEND_ROOT)
        own_legacy_name = f"{DATABASE_NAME_PREFIX}{worktree_hash(_FAKE_BACKEND_ROOT)}"
        other_legacy_name = f"{DATABASE_NAME_PREFIX}{worktree_hash(_OTHER_FAKE_BACKEND_ROOT)}"

        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{own_legacy_name}"'))
            connection.execute(text(f'CREATE DATABASE "{other_legacy_name}"'))
        cleanup_database_names.extend([own_legacy_name, other_legacy_name])

        # Act
        conftest_module._cleanup_legacy_test_databases()

        # Assert
        with admin_engine.connect() as connection:
            assert _database_exists(connection, own_legacy_name) is False
            assert _database_exists(connection, other_legacy_name) is True

    def test_skips_own_worktree_database_with_active_connection(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        # Arrange
        monkeypatch.setattr(conftest_module, "BACKEND_ROOT", _FAKE_BACKEND_ROOT)
        name = f"{DATABASE_NAME_PREFIX}{worktree_hash(_FAKE_BACKEND_ROOT)}"
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        cleanup_database_names.append(name)

        target_engine = create_engine(conftest_module._database_url_for(name))
        target_connection = target_engine.connect()
        try:
            assert _wait_until_has_active_connection(admin_engine, name) is True

            # Act
            conftest_module._cleanup_legacy_test_databases()

            # Assert — 消さない側に倒れる
            with admin_engine.connect() as connection:
                assert _database_exists(connection, name) is True
        finally:
            target_connection.close()
            target_engine.dispose()


class TestFakeBackendRoots:
    """このテスト専用のダミー backend_root が名前空間を占有しないこと（Issue #59）。"""

    def test_derives_a_distinct_path_per_worktree_process_and_label(self) -> None:
        # Arrange
        base = Path("/somewhere/techradar/backend")
        other_base = Path("/elsewhere/techradar/backend")

        # Act / Assert — worktree・プロセス・ラベルのどれが違っても別パスになること
        assert _fake_backend_root(base, "a", 100) != _fake_backend_root(other_base, "a", 100)
        assert _fake_backend_root(base, "a", 100) != _fake_backend_root(base, "a", 200)
        assert _fake_backend_root(base, "a", 100) != _fake_backend_root(base, "b", 100)

    def test_module_level_roots_belong_to_this_worktree_and_process(self) -> None:
        # Arrange / Act / Assert — 固定値ではなく実行中の worktree とプロセスから決まること
        own_pid = os.getpid()
        assert _FAKE_BACKEND_ROOT == _fake_backend_root(conftest_module.BACKEND_ROOT, "a", own_pid)
        assert _OTHER_FAKE_BACKEND_ROOT == _fake_backend_root(
            conftest_module.BACKEND_ROOT, "b", own_pid
        )

    def test_does_not_share_a_hash_with_the_real_worktree(self) -> None:
        # Arrange / Act / Assert — 実 worktree のテスト用 DB を巻き込まないこと
        real_hash = worktree_hash(conftest_module.BACKEND_ROOT)
        assert worktree_hash(_FAKE_BACKEND_ROOT) != real_hash
        assert worktree_hash(_OTHER_FAKE_BACKEND_ROOT) != real_hash
        assert worktree_hash(_FAKE_BACKEND_ROOT) != worktree_hash(_OTHER_FAKE_BACKEND_ROOT)
