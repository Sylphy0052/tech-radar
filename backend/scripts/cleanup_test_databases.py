"""削除済み worktree に紐付くテスト用 DB を掃除するスクリプト（Issue #51）。

Issue #33 で導入した孤児 DB 掃除（`tests/db_process_isolation.py` の
`find_orphaned_database_names` / `find_own_worktree_legacy_database_names`、
呼び出し元は `tests/conftest.py`）は「他 worktree の DB には触らない」ことを
優先し、常に「自分の worktree のハッシュに一致する DB だけ」を対象にする安全側の
設計になっている。その帰結として、worktree 自体を `git worktree remove` 等で
削除すると、そのハッシュを持つ DB はどの worktree の pytest セッションからも
掃除対象にならず、次に同じパスの worktree が作られない限り恒久的に残ってしまう。

このスクリプトは `git worktree list --porcelain` から「現在生存している worktree」
の一覧を取得し、`tests/db_process_isolation.find_database_names_without_live_worktree`
でどの生存 worktree にも属さない DB を洗い出して削除する。pytest 実行時の
掃除ロジック（`tests/conftest.py`）とは判定軸が異なる別経路であり、そちらは
変更しない。

既定は dry-run（削除候補の表示のみ）。実際に削除するには `--apply` を渡す。

    cd backend && uv run python -m scripts.cleanup_test_databases          # dry-run
    cd backend && uv run python -m scripts.cleanup_test_databases --apply  # 実削除

リポジトリルートからは薄いラッパー `scripts/cleanup-test-databases.sh` を使う。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from tests.db_process_isolation import (
    DATABASE_NAME_PREFIX,
    find_database_names_without_live_worktree,
    parse_database_name,
    parse_legacy_database_name,
)

from techradar.config import get_settings

# このファイル（backend/scripts/cleanup_test_databases.py）から見た backend ルート。
# `tests/conftest.py` の `BACKEND_ROOT`（`tests/conftest.py` から見て parents[1]）と
# 同じ深さになるよう揃えている（worktree ハッシュの計算基準を一致させるため）。
BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 破壊的 DDL（DROP DATABASE）を実行してよい接続先の許可リスト。
# `tests/conftest.py` の `_assert_safe_to_drop` と同じ考え方（ローカル / CI サービス
# コンテナ以外は許可しない）だが、`conftest.py` は pytest 用フィクスチャファイルで
# あり、import すると `TEST_DATABASE_NAME` の計算や `WORKER_ENABLED` 環境変数の
# 上書きといった pytest 前提の副作用が standalone 実行にも及んでしまうため、
# ここでは同等の検証を独立して持つ。
ALLOWED_TEST_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})


class WorktreeDiscoveryError(RuntimeError):
    """生存 worktree の一覧取得に失敗したことを表す。

    git が使えない・リポジトリ外から実行された等が原因で、生存 worktree の
    範囲が確定できない状態。安全側に倒し、この例外を受けた呼び出し側は
    何も削除せずに終了する。
    """


@dataclass(frozen=True)
class CleanupPlan:
    """1 回の実行で何をしたか / するかをまとめたレポート。"""

    live_worktree_paths: list[Path]
    existing_database_count: int
    candidates: list[str]
    to_delete: list[str]
    protected_by_connection: list[str]


def _discover_live_worktree_paths() -> list[Path]:
    """`git worktree list --porcelain` から、生存している worktree の絶対パス一覧を返す。

    `git worktree list` はメイン worktree を含む、そのリポジトリに登録済みの
    全 worktree（bare を含む）を返す。git が見つからない、または
    コマンドが失敗する（リポジトリ外から実行された等）場合は
    `WorktreeDiscoveryError` を送出する。
    """
    git_executable = shutil.which("git")
    if git_executable is None:
        message = "gitが見つかりません。PATHを確認してください。"
        raise WorktreeDiscoveryError(message)

    try:
        completed = subprocess.run(  # noqa: S603 — git_executableはshutil.whichで解決済みの絶対パス
            [git_executable, "-C", str(BACKEND_ROOT), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        message = f"git worktree listに失敗しました: {stderr}"
        raise WorktreeDiscoveryError(message) from exc

    paths: list[Path] = []
    for line in completed.stdout.splitlines():
        prefix = "worktree "
        if line.startswith(prefix):
            paths.append(Path(line[len(prefix) :]).resolve())
    return paths


def _validate_database_identifier(name: str) -> None:
    """DROP 文へ識別子として埋め込む直前の最終防衛線。

    `find_database_names_without_live_worktree` が返す名前は、既に
    `parse_database_name` / `parse_legacy_database_name` のいずれかを通過した
    ものだけだが、SQL へ文字列として直接埋め込む処理のため、DROP を実行する
    直前でも独立に再検証する（今後の呼び出し経路の変更に対する防御。
    ここを通らない名前は削除対象に到達できない）。
    """
    if parse_database_name(name) is None and parse_legacy_database_name(name) is None:
        message = f"想定外の形式のDB名のため削除を中止します: {name!r}"
        raise ValueError(message)


def _assert_safe_host(url: str) -> None:
    """破壊的 DDL（DROP DATABASE）を実行してよい接続先かを検証する。

    `DATABASE_URL` が共有 DB やステージングを指していた場合に、同名の DB を
    巻き添えで破壊しうる。ローカルまたは CI のサービスコンテナ以外へは
    接続させない（`tests/conftest.py._assert_safe_to_drop` と同じ考え方）。
    """
    host = (make_url(url).host or "").lower()
    if host not in ALLOWED_TEST_DB_HOSTS:
        message = (
            f"テスト用DBの削除は{sorted(ALLOWED_TEST_DB_HOSTS)}に対してのみ許可しています"
            f"（接続先ホスト: {host or '(未指定)'}）。DATABASE_URLを確認してください。"
        )
        raise RuntimeError(message)


def _admin_engine() -> Engine:
    """維持管理用の `postgres` データベースへ、AUTOCOMMIT で接続するエンジンを返す。

    CREATE / DROP DATABASE はトランザクション内で実行できないため AUTOCOMMIT にする。
    """
    base = make_url(str(get_settings().database_url))
    admin_url = base.set(database="postgres").render_as_string(hide_password=False)
    _assert_safe_host(admin_url)
    return create_engine(admin_url, isolation_level="AUTOCOMMIT")


def _existing_test_database_names(connection: Connection) -> list[str]:
    """このリポジトリのテスト用 DB 名を列挙する（全 worktree・全プロセス分を含む）。"""
    result = connection.execute(
        text("SELECT datname FROM pg_database WHERE datname LIKE :pattern"),
        {"pattern": f"{DATABASE_NAME_PREFIX}%"},
    )
    return [row[0] for row in result]


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


def _drop_database(connection: Connection, database_name: str) -> None:
    """DB を DROP する。呼び出し前に識別子の形式を必ず再検証する。"""
    _validate_database_identifier(database_name)
    connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))


def _build_plan(connection: Connection, live_worktree_paths: list[Path]) -> CleanupPlan:
    """現状の DB 一覧と生存 worktree から、削除候補と実際に削除してよい DB を仕分ける。

    削除候補（`find_database_names_without_live_worktree` の結果）のうち、
    接続が残っている DB は削除しない（他プロセスが使用中の可能性を否定できない
    ため、消さない側に倒す）。
    """
    existing_names = _existing_test_database_names(connection)
    live_backend_roots = [path / "backend" for path in live_worktree_paths]
    candidates = find_database_names_without_live_worktree(
        existing_names,
        live_backend_roots=live_backend_roots,
    )

    to_delete: list[str] = []
    protected_by_connection: list[str] = []
    for name in candidates:
        if _has_active_connections(connection, name):
            protected_by_connection.append(name)
        else:
            to_delete.append(name)

    return CleanupPlan(
        live_worktree_paths=live_worktree_paths,
        existing_database_count=len(existing_names),
        candidates=candidates,
        to_delete=to_delete,
        protected_by_connection=protected_by_connection,
    )


def _print_report(plan: CleanupPlan, *, applied: bool) -> None:
    """人が読める形で削除候補・保護対象を報告する。"""
    print(f"[cleanup-test-databases] 生存worktree: {len(plan.live_worktree_paths)}件")
    for path in plan.live_worktree_paths:
        print(f"  - {path}")

    print(f"[cleanup-test-databases] 既存のテスト用DB: {plan.existing_database_count}件")
    print(
        f"[cleanup-test-databases] 削除候補（生存worktreeに属さないDB）: {len(plan.candidates)}件"
    )

    if plan.to_delete:
        action_label = "削除しました" if applied else "削除対象（--applyで削除）"
        print(f"[cleanup-test-databases] {action_label}: {len(plan.to_delete)}件")
        for name in plan.to_delete:
            print(f"  - {name}")
    else:
        print("[cleanup-test-databases] 削除対象なし")

    if plan.protected_by_connection:
        print(
            "[cleanup-test-databases] 保護（接続が残っているため削除しない）: "
            f"{len(plan.protected_by_connection)}件"
        )
        for name in plan.protected_by_connection:
            print(f"  - {name}")

    if not applied and plan.to_delete:
        print(
            "[cleanup-test-databases] dry-runのため実際には削除していません。"
            "--applyを指定すると上記を削除します。"
        )


def main(argv: list[str] | None = None) -> int:
    """生存 worktree に属さないテスト用 DB を検出し、`--apply` 指定時のみ削除する。

    Returns:
        終了コード。`git worktree list` に失敗した場合は 1、それ以外は 0。
    """
    arguments = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="削除済みworktreeに紐付くテスト用DBを掃除する（既定はdry-run）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際にDBを削除する。指定しない場合はdry-run（候補の表示のみで削除しない）。",
    )
    args = parser.parse_args(arguments)

    try:
        live_worktree_paths = _discover_live_worktree_paths()
    except WorktreeDiscoveryError as exc:
        print(f"[cleanup-test-databases][ERROR] {exc}", file=sys.stderr)
        print("[cleanup-test-databases][ERROR] 何も削除せずに終了します。", file=sys.stderr)
        return 1

    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as connection:
            plan = _build_plan(connection, live_worktree_paths)
            if args.apply:
                for name in plan.to_delete:
                    _drop_database(connection, name)
            _print_report(plan, applied=args.apply)
    finally:
        admin_engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
