"""`backend/scripts/cleanup_test_databases.py` のテスト（Issue #51 self review 対応）。

このスクリプトは `DROP DATABASE` を含む破壊的操作を行うにもかかわらず、Issue #51 の
self review で「手動実行ログでしか検証されていない」と指摘された。`tests/conftest.py`
の破壊的操作を検証する `tests/test_conftest_db_cleanup.py`（Issue #33 self review 対応）と
同じ枠組みに揃え、以下を検証する。

- `_discover_live_worktree_paths` の porcelain 解析（`subprocess` をモック）
- `git` が見つからない・`git worktree list` が失敗した場合の `WorktreeDiscoveryError`
- 自分自身の worktree が生存一覧に含まれない場合の `WorktreeDiscoveryError`（Issue #63）
- `_validate_database_identifier` / `_assert_safe_host` の拒否条件
- 実 DB を使った `_build_plan` の仕分け（生存worktreeに属さないDB／接続が残っているDB／
  PIDが生存しているDB／作成から間もないDB。いずれもIssue #63で追加した保護）
- 実 DB を使った `--apply` の有無による DROP の有無（`_apply_plan`）

実 DB を使うテストは `test_conftest_db_cleanup.py` と同じ方針で、実運用の worktree
ハッシュとは無関係な名前空間（`_FAKE_*_ROOT`）を使い、本番の掃除ロジック（他プロセスが
このリポジトリの本来の worktree ハッシュで実行する掃除）と自己干渉しないようにする。
作った DB はテスト自身が必ず後始末する（`cleanup_database_names` フィクスチャが
`finally` 相当のティアダウンで DROP する。失敗時も残さない）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scripts import cleanup_test_databases as cleanup_module
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from techradar.config import get_settings
from tests.db_process_isolation import build_database_name, worktree_hash
from tests.fake_worktree_roots import ANOTHER_DEAD_PID, DEAD_PID, fake_worktree_path

# このテスト専用のダミー repo root。本番の掃除ロジックが使う worktree ハッシュとは
# 別の名前空間になるため、実行中に他プロセスの掃除へ拾われることがない。実 worktree と
# 実プロセスから派生させる理由は `tests/fake_worktree_roots` を参照（Issue #59）。
_FAKE_LIVE_REPO_ROOT = fake_worktree_path(
    cleanup_module.BACKEND_ROOT.parent, "issue-51-live", os.getpid()
)
_FAKE_ORPHANED_REPO_ROOT = fake_worktree_path(
    cleanup_module.BACKEND_ROOT.parent, "issue-51-orphaned", os.getpid()
)
# Issue #63 で追加した保護（PID生存・作成直後）専用のダミー repo root。
_FAKE_ISSUE_63_REPO_ROOT = fake_worktree_path(
    cleanup_module.BACKEND_ROOT.parent, "issue-63", os.getpid()
)


def _own_worktree_root() -> Path:
    """このテストを実行している worktree 自身のルートパス。

    Issue #63 で `_discover_live_worktree_paths` が「自分自身が生存worktree一覧に
    含まれるはず」を検証するようになったため、porcelain 出力をモックするテストは
    このパスを含めないと（本来の意図と無関係に）`WorktreeDiscoveryError` になる。
    """
    return cleanup_module.BACKEND_ROOT.parent.resolve()


def _own_worktree_porcelain_block() -> str:
    """自分自身の worktree を表す porcelain ブロック（末尾に空行区切りを含む）。"""
    return (
        f"worktree {_own_worktree_root()}\n"
        "HEAD 0123456789abcdef0123456789abcdef01234567\n"
        "branch refs/heads/main\n"
        "\n"
    )


class _FakeCompletedProcess:
    """`subprocess.run` の戻り値を模した最小限のスタブ。"""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""


def _database_url_for(database_name: str) -> str:
    """設定の接続情報を流用し、データベース名だけ差し替えた URL を返す。"""
    base = make_url(str(get_settings().database_url))
    return base.set(database=database_name).render_as_string(hide_password=False)


def _wait_until_has_active_connection(
    admin_engine: Engine, database_name: str, *, timeout_seconds: float = 2.0
) -> bool:
    """`database_name` への接続が `pg_stat_activity` に現れるまで短時間ポーリングする。

    クライアント側で接続が確立しても、PostgreSQL 側のバックエンドプロセスが
    `pg_stat_activity` に載るまでに数十ms程度のラグが生じることがある。
    「接続が残っている DB は消さない」を検証するテストでこのラグを踏むと、
    掃除関数がまだ接続を検知できていないだけなのに安全弁が反転したように
    誤検出してしまうため、確認前に接続が確実に見えている状態まで待つ。
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        with admin_engine.connect() as connection:
            if cleanup_module._has_active_connections(connection, database_name):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class TestDiscoverLiveWorktreePaths:
    """`_discover_live_worktree_paths` の porcelain 解析（`subprocess` をモック）。"""

    def test_excludes_prunable_and_includes_live_worktree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange — 生存worktree・prunable済みworktree（どちらも実在するディレクトリ）
        live_path = tmp_path / "live-worktree"
        live_path.mkdir()
        prunable_path = tmp_path / "prunable-worktree"
        prunable_path.mkdir()

        stdout = (
            _own_worktree_porcelain_block() + f"worktree {live_path}\n"
            "HEAD 0123456789abcdef0123456789abcdef01234567\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {prunable_path}\n"
            "HEAD 0123456789abcdef0123456789abcdef01234567\n"
            "detached\n"
            "prunable gitdir file points to non-existent location\n"
        )
        monkeypatch.setattr(cleanup_module.shutil, "which", lambda _name: "/usr/bin/git")
        monkeypatch.setattr(
            cleanup_module.subprocess,
            "run",
            lambda *args, **kwargs: _FakeCompletedProcess(stdout=stdout),
        )

        # Act
        paths = cleanup_module._discover_live_worktree_paths()

        # Assert — prunable側は除外され、自分自身と通常のworktreeだけが残る
        assert paths == [_own_worktree_root(), live_path.resolve()]

    def test_ignores_non_worktree_lines_without_breaking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange — HEAD/branch/locked等の付随行が混ざっていても解析できること
        live_path = tmp_path / "locked-worktree"
        live_path.mkdir()
        stdout = (
            _own_worktree_porcelain_block() + f"worktree {live_path}\n"
            "HEAD 0123456789abcdef0123456789abcdef01234567\n"
            "branch refs/heads/feature\n"
            "locked manually locked for testing\n"
        )
        monkeypatch.setattr(cleanup_module.shutil, "which", lambda _name: "/usr/bin/git")
        monkeypatch.setattr(
            cleanup_module.subprocess,
            "run",
            lambda *args, **kwargs: _FakeCompletedProcess(stdout=stdout),
        )

        # Act
        paths = cleanup_module._discover_live_worktree_paths()

        # Assert
        assert paths == [_own_worktree_root(), live_path.resolve()]

    def test_excludes_worktree_when_path_does_not_exist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange — prunable行は無いが、パス自体が実在しない（rm -rfされた等）
        removed_path = tmp_path / "removed-worktree"
        stdout = (
            _own_worktree_porcelain_block() + f"worktree {removed_path}\n"
            "HEAD 0123456789abcdef0123456789abcdef01234567\n"
            "detached\n"
        )
        monkeypatch.setattr(cleanup_module.shutil, "which", lambda _name: "/usr/bin/git")
        monkeypatch.setattr(
            cleanup_module.subprocess,
            "run",
            lambda *args, **kwargs: _FakeCompletedProcess(stdout=stdout),
        )

        # Act
        paths = cleanup_module._discover_live_worktree_paths()

        # Assert — 自分自身は残り、実在しないパスは安全側（生存とみなさない）に倒す
        assert paths == [_own_worktree_root()]

    def test_raises_when_own_worktree_is_missing_from_live_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """自分自身の worktree が生存一覧に含まれない場合、`WorktreeDiscoveryError`
        を送出する（Issue #63）。`git worktree list` の結果が実態と乖離している
        （壊れたメタデータ等）ことの兆候であり、他の worktree の生死も信用できない。
        """
        # Arrange — 自分自身を含まない出力
        other_path = tmp_path / "some-other-worktree"
        other_path.mkdir()
        stdout = (
            f"worktree {other_path}\n"
            "HEAD 0123456789abcdef0123456789abcdef01234567\n"
            "branch refs/heads/main\n"
        )
        monkeypatch.setattr(cleanup_module.shutil, "which", lambda _name: "/usr/bin/git")
        monkeypatch.setattr(
            cleanup_module.subprocess,
            "run",
            lambda *args, **kwargs: _FakeCompletedProcess(stdout=stdout),
        )

        # Act & Assert
        with pytest.raises(cleanup_module.WorktreeDiscoveryError):
            cleanup_module._discover_live_worktree_paths()

    def test_raises_when_git_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(cleanup_module.shutil, "which", lambda _name: None)

        # Act & Assert
        with pytest.raises(cleanup_module.WorktreeDiscoveryError):
            cleanup_module._discover_live_worktree_paths()

    def test_raises_when_git_worktree_list_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        def _raise(*args: Any, **kwargs: Any) -> None:
            raise cleanup_module.subprocess.CalledProcessError(
                returncode=128,
                cmd=["git", "worktree", "list", "--porcelain"],
                stderr="fatal: not a git repository",
            )

        monkeypatch.setattr(cleanup_module.shutil, "which", lambda _name: "/usr/bin/git")
        monkeypatch.setattr(cleanup_module.subprocess, "run", _raise)

        # Act & Assert
        with pytest.raises(cleanup_module.WorktreeDiscoveryError):
            cleanup_module._discover_live_worktree_paths()

    def test_raises_when_worktree_path_contains_newline(self) -> None:
        """パスに改行が含まれると worktree 行が分断される。切れたパスを拾わず止まること。

        git はパスをエスケープせずに出力する。切り詰めたパスを生存 worktree として
        扱うと、本物のパスのハッシュが生存集合へ入らず、現役の DB が削除候補に
        入ってしまう（消してはいけないものを消す方向へ効く）。
        """
        # Arrange — 実際に `git worktree add` した際の出力と同じ形。`line` はパスの続き。
        porcelain_output = (
            "worktree /tmp/x/repo\n"
            "HEAD 4924ee83f9626eefdcd78435a790fff4ad66070d\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /tmp/x/wt-new\n"
            "line\n"
            "HEAD 4924ee83f9626eefdcd78435a790fff4ad66070d\n"
            "branch refs/heads/br-newline\n"
            "\n"
        )

        # Act & Assert
        with pytest.raises(cleanup_module.WorktreeDiscoveryError):
            cleanup_module._parse_worktree_porcelain(porcelain_output)


class TestValidateDatabaseIdentifier:
    """`_validate_database_identifier` の拒否条件。"""

    def test_rejects_unexpected_name(self) -> None:
        with pytest.raises(ValueError, match="想定外の形式"):
            cleanup_module._validate_database_identifier("not-a-managed-database-name")


class TestAssertSafeHost:
    """`_assert_safe_host` の拒否条件。"""

    def test_rejects_disallowed_host(self) -> None:
        with pytest.raises(RuntimeError, match="許可"):
            cleanup_module._assert_safe_host(
                "postgresql://user:pass@evil-host.example.com:5432/postgres"
            )


@pytest.fixture
def admin_engine() -> Iterator[Engine]:
    """維持管理用 DB への AUTOCOMMIT エンジン。テスト終了時に必ず破棄する。"""
    engine = cleanup_module._admin_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def cleanup_database_names(admin_engine: Engine) -> Iterator[list[str]]:
    """テストが作った一時 DB 名を集めるリスト。

    テスト終了時（失敗時も含む）に、集めた名前をすべて強制切断のうえ DROP する。
    """
    names: list[str] = []
    try:
        yield names
    finally:
        with admin_engine.connect() as connection:
            for name in names:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


class TestBuildPlan:
    """`_build_plan` の実DBに対する振る舞い。"""

    def test_separates_candidates_by_live_worktree_and_active_connection(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        # Arrange
        live_backend_root = _FAKE_LIVE_REPO_ROOT / "backend"
        orphaned_backend_root = _FAKE_ORPHANED_REPO_ROOT / "backend"

        live_db = build_database_name(live_backend_root, os.getpid())
        # 自分（テスト実行プロセス）の PID を使うと、Issue #63 で追加した
        # PID生存保護に引っかかり `to_delete` に入らなくなる。実在しえない
        # PID（Issue #59）を使い、PID保護ではなく接続の有無で仕分けさせる。
        candidate_no_conn_db = build_database_name(orphaned_backend_root, ANOTHER_DEAD_PID)
        candidate_with_conn_db = build_database_name(orphaned_backend_root, DEAD_PID)
        names = [live_db, candidate_no_conn_db, candidate_with_conn_db]

        with admin_engine.connect() as connection:
            for name in names:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
        cleanup_database_names.extend(names)

        target_engine = create_engine(_database_url_for(candidate_with_conn_db))
        target_connection = target_engine.connect()
        try:
            assert _wait_until_has_active_connection(admin_engine, candidate_with_conn_db) is True

            # Act — live_backend_rootだけを生存worktreeとして渡す
            # （作成直後DBの保護＝Issue #63分はこのテストの対象外のため無効化する）
            with admin_engine.connect() as connection:
                plan = cleanup_module._build_plan(
                    connection, [_FAKE_LIVE_REPO_ROOT], min_age_minutes=0
                )

            # Assert
            assert live_db not in plan.candidates
            assert candidate_no_conn_db in plan.to_delete
            assert candidate_with_conn_db in plan.protected_by_connection
            assert candidate_with_conn_db not in plan.to_delete
        finally:
            target_connection.close()
            target_engine.dispose()


class TestBuildPlanPidProtection:
    """PID生存保護（Issue #63）: DB名に埋め込まれたPIDが生存していれば削除しない。"""

    def test_protects_database_whose_embedded_pid_is_alive(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        # Arrange — 自分自身（テスト実行中のプロセス）の PID を使う。
        # 別セッションのpytestが今まさに使っているDBを模す。
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, os.getpid())
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        # Act — 生存worktreeを空にして必ず候補に挙げつつ、作成直後DBの保護
        # （Issue #63の別項目）は無効化してPID保護だけを見る
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)

        # Assert
        assert db_name in plan.candidates
        assert db_name in plan.protected_by_alive_pid
        assert db_name not in plan.to_delete


class TestBuildPlanRecentCreationProtection:
    """作成直後DBの保護（Issue #63）。"""

    def test_protects_freshly_created_database_within_grace_period(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        # Arrange — PID保護に引っかからないよう実在しえないPID（Issue #59）を使う
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        # Act — 既定の猶予期間（`DEFAULT_MIN_AGE_MINUTES`分）のまま呼ぶ。
        # 作成直後なので、実際の pg_stat_file 経由の作成時刻でも保護されるはず。
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [])

        # Assert
        assert db_name in plan.candidates
        assert db_name in plan.protected_by_recent_creation
        assert db_name not in plan.to_delete

    def test_deletes_database_older_than_grace_period(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        """猶予期間より古い作成時刻であれば、従来どおり削除対象になること。

        実際に猶予期間分（既定10分）待つのは非現実的なため、`_database_creation_times`
        （`pg_stat_file` 経由の一括取得）をモックして、猶予期間より古い時刻を返す。
        """
        # Arrange — PID保護に引っかからないよう実在しえないPIDを使う
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, ANOTHER_DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        old_creation_time = datetime.now(UTC) - timedelta(
            minutes=cleanup_module.DEFAULT_MIN_AGE_MINUTES + 1
        )
        monkeypatch.setattr(
            cleanup_module,
            "_database_creation_times",
            lambda _connection: {db_name: old_creation_time},
        )

        # Act
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [])

        # Assert — 猶予期間より古いため、保護されず削除対象のまま
        assert db_name in plan.to_delete
        assert db_name not in plan.protected_by_recent_creation

    def test_min_age_minutes_zero_disables_protection(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        """`--min-age-minutes 0`（`min_age_minutes=0`）相当では、作成直後でも
        保護されずに削除対象になること。
        """
        # Arrange
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, ANOTHER_DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        # Act — 作成直後だが、猶予期間そのものを無効化して呼ぶ
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)

        # Assert
        assert db_name in plan.to_delete
        assert db_name not in plan.protected_by_recent_creation

    def test_boundary_at_exactly_min_age_minutes_is_not_protected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        """猶予期間の境界値: ちょうど`min_age_minutes`分前に作られた扱いにしても
        保護されないこと（実装の比較が`<`であることの固定、Issue #63 self review）。

        `_build_plan`内で計算される`datetime.now(UTC)`は、この`created_at`を
        用意した時点より必ず後になるため、実際の経過時間は境界をまたいで
        `min_age_minutes`以上になる。
        """
        # Arrange — PID保護に引っかからないよう実在しえないPIDを使う
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, ANOTHER_DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        min_age_minutes = 5
        boundary_creation_time = datetime.now(UTC) - timedelta(minutes=min_age_minutes)
        monkeypatch.setattr(
            cleanup_module,
            "_database_creation_times",
            lambda _connection: {db_name: boundary_creation_time},
        )

        # Act
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=min_age_minutes)

        # Assert
        assert db_name not in plan.protected_by_recent_creation
        assert db_name in plan.to_delete


class TestBuildPlanPidTrustWindow:
    """PID信用期限（Issue #63）: 作成から`PID_TRUST_WINDOW_HOURS`時間を超えたDBは、
    DB名に埋め込まれたPIDが生存していても、その保護をスキップすること。
    """

    def test_does_not_trust_alive_pid_after_trust_window_expires(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        # Arrange — 自分自身（生存中）のPIDを使う。PID再利用でたまたま生存中の
        # 別プロセスに割り当たったケースを模す。作成時刻だけ信用期限より古く見せる。
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, os.getpid())
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        old_creation_time = datetime.now(UTC) - timedelta(
            hours=cleanup_module.PID_TRUST_WINDOW_HOURS, minutes=1
        )
        monkeypatch.setattr(
            cleanup_module,
            "_database_creation_times",
            lambda _connection: {db_name: old_creation_time},
        )

        # Act — 猶予期間保護（Issue #63の別項目）は無効化し、PID信用期限だけを見る
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)

        # Assert — PIDは生存しているが信用期限切れのため保護されず、削除対象になる
        assert db_name not in plan.protected_by_alive_pid
        assert db_name in plan.to_delete

    def test_trusts_alive_pid_within_trust_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        # Arrange — 信用期限（`PID_TRUST_WINDOW_HOURS`時間）ぎりぎり内側の作成時刻
        backend_root = _FAKE_ISSUE_63_REPO_ROOT / "backend"
        db_name = build_database_name(backend_root, os.getpid())
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        recent_creation_time = datetime.now(UTC) - timedelta(
            hours=cleanup_module.PID_TRUST_WINDOW_HOURS, minutes=-1
        )
        monkeypatch.setattr(
            cleanup_module,
            "_database_creation_times",
            lambda _connection: {db_name: recent_creation_time},
        )

        # Act
        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)

        # Assert — 信用期限内のため、従来どおりPID生存で保護される
        assert db_name in plan.protected_by_alive_pid
        assert db_name not in plan.to_delete


class TestDatabaseCreationTimes:
    """`_database_creation_times`が例外を捕まえて`None`を返す経路（Issue #63）。

    `_build_plan`はこの`None`を「作成時刻不明」として扱い、作成直後DBの猶予期間
    保護とPID信用期限の判定を無効化する（従来どおりPID生存を信用する安全側）。
    """

    def test_returns_none_and_warns_when_query_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Arrange — 実接続のexecuteだけをエラーに差し替える
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise SQLAlchemyError("boom")

        with admin_engine.connect() as connection:
            monkeypatch.setattr(connection, "execute", _raise)

            # Act
            result = cleanup_module._database_creation_times(connection)

        # Assert
        assert result is None
        assert "作成時刻の取得に失敗" in capsys.readouterr().err


class TestCheckMaxDelete:
    """`--apply`時のサーキットブレーカー（Issue #63）: 削除候補が上限を超えたら中止する。"""

    @staticmethod
    def _plan_with_to_delete_count(count: int) -> cleanup_module.CleanupPlan:
        names = [f"techradar_test_deadbeef_{i + 1}" for i in range(count)]
        return cleanup_module.CleanupPlan(
            live_worktree_paths=[],
            existing_database_count=count,
            candidates=names,
            to_delete=names,
            protected_by_connection=[],
            protected_by_alive_pid=[],
            protected_by_recent_creation=[],
        )

    def test_raises_when_to_delete_exceeds_max_delete(self) -> None:
        # Arrange
        plan = self._plan_with_to_delete_count(cleanup_module.DEFAULT_MAX_DELETE + 1)

        # Act / Assert
        with pytest.raises(cleanup_module.TooManyDeletionsError):
            cleanup_module._check_max_delete(plan, cleanup_module.DEFAULT_MAX_DELETE)

    def test_does_not_raise_when_to_delete_equals_max_delete(self) -> None:
        # Arrange
        plan = self._plan_with_to_delete_count(cleanup_module.DEFAULT_MAX_DELETE)

        # Act / Assert — 例外が出なければOK（境界＝上限ちょうどは許容する）
        cleanup_module._check_max_delete(plan, cleanup_module.DEFAULT_MAX_DELETE)

    def test_zero_means_unlimited(self) -> None:
        # Arrange
        plan = self._plan_with_to_delete_count(cleanup_module.DEFAULT_MAX_DELETE * 100)

        # Act / Assert — 例外が出なければOK
        cleanup_module._check_max_delete(plan, 0)


def _plan_limited_to(plan: cleanup_module.CleanupPlan, name: str) -> cleanup_module.CleanupPlan:
    """`to_delete` をこのテストが作った DB 1 件だけに絞った plan を返す。

    `_build_plan` へ空の生存 worktree 一覧を渡すと、クラスタ上の全テスト用 DB が
    削除候補になる。そのまま `_apply_plan` へ渡すと、同時に走っている別セッションの
    テストが使う DB まで本当に DROP してしまう（新形式で PID が生きているものは
    保護されるが、旧形式や実在しない PID を使うダミーは保護に掛からない）。
    削除の対象を自分が作った DB へ限定してから `_apply_plan` を呼ぶ（Issue #63）。
    """
    return cleanup_module.CleanupPlan(
        live_worktree_paths=plan.live_worktree_paths,
        existing_database_count=plan.existing_database_count,
        candidates=[name],
        to_delete=[name] if name in plan.to_delete else [],
        protected_by_connection=[],
        protected_by_alive_pid=[],
        protected_by_recent_creation=[],
    )


class TestApplyPlan:
    """`--apply`（`_apply_plan`）の実DBに対する振る舞い。"""

    def test_dry_run_does_not_drop(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        # Arrange — 自分の PID を使うとIssue #63のPID生存保護に引っかかるため
        # 実在しえないPID（Issue #59）を使う。作成直後DBの保護（Issue #63）も
        # このテストの対象外のため無効化する。
        orphaned_backend_root = _FAKE_ORPHANED_REPO_ROOT / "backend"
        db_name = build_database_name(orphaned_backend_root, DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)
        assert db_name in plan.to_delete

        # Act — dry-run: _apply_planを呼ばない（mainの--apply未指定時の経路と同じ）

        # Assert — DBは残ったまま
        with admin_engine.connect() as connection:
            result = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            )
            assert result.first() is not None

    def test_apply_drops_candidate(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        # Arrange — 理由はtest_dry_run_does_not_dropと同じ
        orphaned_backend_root = _FAKE_ORPHANED_REPO_ROOT / "backend"
        db_name = build_database_name(orphaned_backend_root, DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)
            assert db_name in plan.to_delete

            # Act
            applied_plan = cleanup_module._apply_plan(connection, _plan_limited_to(plan, db_name))

        # Assert
        assert db_name in applied_plan.to_delete
        assert db_name not in applied_plan.protected_by_connection
        with admin_engine.connect() as connection:
            result = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            )
            assert result.first() is None

    def test_apply_skips_database_whose_pid_becomes_alive_before_drop(
        self,
        monkeypatch: pytest.MonkeyPatch,
        admin_engine: Engine,
        cleanup_database_names: list[str],
    ) -> None:
        """DROP直前のPID再確認分岐: `_build_plan`時点では削除対象でも、DROP直前に
        `pid_is_alive`がTrueを返せば削除せず`protected_by_alive_pid`へ回ること。
        """
        # Arrange — 理由はtest_dry_run_does_not_dropと同じ
        orphaned_backend_root = _FAKE_ORPHANED_REPO_ROOT / "backend"
        db_name = build_database_name(orphaned_backend_root, DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)
        assert db_name in plan.to_delete

        # Act — DROP直前にPIDが生存扱いになる状況を再現する
        monkeypatch.setattr(cleanup_module, "pid_is_alive", lambda _pid: True)
        with admin_engine.connect() as connection:
            applied_plan = cleanup_module._apply_plan(connection, _plan_limited_to(plan, db_name))

        # Assert — 削除されず、保護対象へ回る
        assert db_name not in applied_plan.to_delete
        assert db_name in applied_plan.protected_by_alive_pid
        with admin_engine.connect() as connection:
            result = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            )
            assert result.first() is not None

    def test_apply_skips_database_that_gained_connection_after_planning(
        self, admin_engine: Engine, cleanup_database_names: list[str]
    ) -> None:
        """TOCTOU対策: `_build_plan`時点では接続が無くても、DROP直前に接続が
        張られていれば削除せずスキップし、`protected_by_connection`へ回ること。
        """
        # Arrange — 理由はtest_dry_run_does_not_dropと同じ
        orphaned_backend_root = _FAKE_ORPHANED_REPO_ROOT / "backend"
        db_name = build_database_name(orphaned_backend_root, DEAD_PID)
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{db_name}"'))
        cleanup_database_names.append(db_name)

        with admin_engine.connect() as connection:
            plan = cleanup_module._build_plan(connection, [], min_age_minutes=0)
        assert db_name in plan.to_delete

        # Act — plan確定後、DROP実行前に接続を張ることで競合状態を再現する
        target_engine = create_engine(_database_url_for(db_name))
        target_connection = target_engine.connect()
        try:
            assert _wait_until_has_active_connection(admin_engine, db_name) is True

            with admin_engine.connect() as connection:
                applied_plan = cleanup_module._apply_plan(
                    connection, _plan_limited_to(plan, db_name)
                )

            # Assert — 削除されず、保護対象へ回る
            assert db_name not in applied_plan.to_delete
            assert db_name in applied_plan.protected_by_connection
        finally:
            target_connection.close()
            target_engine.dispose()

        with admin_engine.connect() as connection:
            result = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}
            )
            assert result.first() is not None


class TestFakeRepoRoots:
    """このテスト専用のダミー repo root が名前空間を占有しないこと（Issue #59）。

    `fake_worktree_path` 自体の性質は `tests/test_fake_worktree_roots` で検証する。
    ここでは、このファイルのダミーがそれを実際に通っているかだけを見る。
    """

    def test_module_level_roots_vary_by_worktree_and_process(self) -> None:
        # Arrange / Act / Assert — 固定値ではなく実行中の worktree とプロセスから決まること
        real_repo_root = cleanup_module.BACKEND_ROOT.parent
        for root in (_FAKE_LIVE_REPO_ROOT, _FAKE_ORPHANED_REPO_ROOT, _FAKE_ISSUE_63_REPO_ROOT):
            assert root.parent == real_repo_root.parent
            assert real_repo_root.name in root.name
            assert str(os.getpid()) in root.name

    def test_does_not_share_a_hash_with_the_real_worktree(self) -> None:
        # Arrange / Act / Assert — 実 worktree のテスト用 DB を巻き込まないこと
        real_hash = worktree_hash(cleanup_module.BACKEND_ROOT)
        assert worktree_hash(_FAKE_LIVE_REPO_ROOT / "backend") != real_hash
        assert worktree_hash(_FAKE_ORPHANED_REPO_ROOT / "backend") != real_hash
        assert worktree_hash(_FAKE_ISSUE_63_REPO_ROOT / "backend") != real_hash
        assert worktree_hash(_FAKE_LIVE_REPO_ROOT / "backend") != worktree_hash(
            _FAKE_ORPHANED_REPO_ROOT / "backend"
        )
