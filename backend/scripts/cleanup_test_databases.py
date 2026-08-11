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

# `git worktree list` に許す秒数。ローカルのメタデータを読むだけなので本来は一瞬で終わる。
# 上限を置くのは、ネットワーク越しの作業ディレクトリや認証情報ヘルパー待ちでハングした
# ときに、無人実行（skill 経由など）が止まったまま残らないようにするため。
GIT_TIMEOUT_SECONDS = 30


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

    `git worktree remove` を経ずに `rm -rf` 等でディレクトリだけ消された worktree は、
    登録自体は残ったまま porcelain 出力の当該ブロックに `prunable <理由>` 行が付く。
    そのような worktree を生存扱いすると、そこに紐付くテスト用 DB がこのスクリプトの
    掃除対象から外れてしまう（このスクリプトの主目的を取りこぼす）ため、`prunable` 行を
    持つブロックは除外する。あわせて、`prunable` 行の有無に関わらずパスの実在も確認し、
    存在しないものは安全側（生存とみなさない）に倒す。`git worktree prune` は実行しない
    （git のメタデータを書き換えるのはこのスクリプトの責務外で、DB の掃除だけを行う）。
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
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"git worktree listが{GIT_TIMEOUT_SECONDS}秒以内に応答しませんでした。"
        raise WorktreeDiscoveryError(message) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        message = f"git worktree listに失敗しました: {stderr}"
        raise WorktreeDiscoveryError(message) from exc

    return [
        path
        for path, prunable in _parse_worktree_porcelain(completed.stdout)
        if not prunable and path.exists()
    ]


def _parse_worktree_porcelain(porcelain_output: str) -> list[tuple[Path, bool]]:
    """`git worktree list --porcelain` の出力を解析し、`(パス, prunableか)` の一覧を返す。

    porcelain 形式はブロック（空行区切り）単位で 1 worktree を表し、各ブロックは
    必ず `worktree <path>` 行から始まり、その次の行は `HEAD <sha>` / `bare` /
    `detached` のいずれかになる。`prunable` はそのブロック内のどこかに
    `prunable <理由>` という行として現れる。それ以外の行（`branch`, `locked` 等）は
    無視するため、混ざってもパース結果に影響しない。

    `worktree <path>` の直後が想定の行で始まっていない場合は、出力の構造が想定と
    違うとみなして `WorktreeDiscoveryError` を送出する。git はパスをエスケープせず
    そのまま出すため、パスに改行が含まれると `worktree` 行がその位置で分断され、
    途中で切れたパスを拾ってしまう。切れたパスのハッシュは本物と一致しないので、
    その worktree は「生存していない」と判定され、現役の DB が削除候補に入る。
    取りこぼしがそのまま「消してはいけないものを消す」方向へ効くため、
    解釈できない出力を前にしたら何もせず止まる。
    """
    worktree_prefix = "worktree "
    prunable_prefix = "prunable"
    # `worktree <path>` の次に来ることが porcelain 形式で保証されている行。
    expected_after_worktree = ("HEAD ", "bare", "detached")

    entries: list[tuple[Path, bool]] = []
    current_path: Path | None = None
    current_prunable = False
    expecting_header = False

    def flush() -> None:
        nonlocal current_path, current_prunable
        if current_path is not None:
            entries.append((current_path, current_prunable))
        current_path = None
        current_prunable = False

    for line in porcelain_output.splitlines():
        if expecting_header:
            expecting_header = False
            if not line.startswith(expected_after_worktree):
                message = (
                    "git worktree listの出力を解釈できません"
                    f"（worktree行の次に想定外の行がありました: {line!r}）。"
                    "パスに改行が含まれている可能性があります。何も削除せずに終了します。"
                )
                raise WorktreeDiscoveryError(message)

        if line == "":
            flush()
        elif line.startswith(worktree_prefix):
            flush()  # 空行区切りが無いまま次のブロックが始まった場合への防御
            current_path = Path(line[len(worktree_prefix) :]).resolve()
            expecting_header = True
        elif line.startswith(prunable_prefix):
            current_prunable = True
    flush()

    return entries


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


def _apply_plan(connection: Connection, plan: CleanupPlan) -> CleanupPlan:
    """`plan.to_delete` を実際に DROP し、結果を反映した新しい `CleanupPlan` を返す。

    `_build_plan` が接続の有無を確認してから、この関数が実際に DROP するまでには
    レポート表示等の時間差があり、その間に新しい接続が張られる可能性がある
    （TOCTOU）。`backend/tests/conftest.py` の `_cleanup_orphaned_test_databases` は
    確認直後にその場で DROP しており窓が小さいが、このスクリプトは `_build_plan` と
    DROP が分離しているため、DROP の直前に `_has_active_connections` を再確認する。
    その時点で接続が残っていた DB は削除せず、`protected_by_connection` へ回して
    （既存の DB と同じ理由のため合流させる）レポートに反映されるようにする。

    `CleanupPlan` は不変（frozen dataclass）のため、既存のインスタンスを書き換えず
    新しいインスタンスを作って返す。
    """
    dropped: list[str] = []
    skipped_at_drop: list[str] = []
    for name in plan.to_delete:
        if _has_active_connections(connection, name):
            skipped_at_drop.append(name)
            continue
        _drop_database(connection, name)
        dropped.append(name)

    if not skipped_at_drop:
        return plan

    return CleanupPlan(
        live_worktree_paths=plan.live_worktree_paths,
        existing_database_count=plan.existing_database_count,
        candidates=plan.candidates,
        to_delete=dropped,
        protected_by_connection=[*plan.protected_by_connection, *skipped_at_drop],
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

    `git worktree list` の失敗だけは、生存範囲が確定しないという掃除固有の事情を
    伝えたいため捕捉してメッセージを出す。接続先ホストの検証や DB への接続に失敗した
    場合は捕捉せず、そのまま送出する（いずれも DROP を実行する前に落ちる）。

    Returns:
        終了コード。`git worktree list` に失敗した場合は 1、それ以外は 0。
    """
    parser = argparse.ArgumentParser(
        description="削除済みworktreeに紐付くテスト用DBを掃除する（既定はdry-run）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際にDBを削除する。指定しない場合はdry-run（候補の表示のみで削除しない）。",
    )
    args = parser.parse_args(argv)

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
                plan = _apply_plan(connection, plan)
            _print_report(plan, applied=args.apply)
    finally:
        admin_engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
