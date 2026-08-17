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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from tests.db_process_isolation import (
    DATABASE_NAME_PREFIX,
    find_database_names_without_live_worktree,
    parse_database_name,
    parse_legacy_database_name,
    pid_is_alive,
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

# 作成から間もない DB を保護する既定の猶予期間（分）。PID 生存判定は「DB 名に
# 埋め込まれた PID が今も動いているか」しか見ないため、PID の再利用やこの
# スクリプト自身の判定漏れなど、想定していない経路で誤って削除候補に挙がった
# 場合でも、作られたばかりの DB であれば時間経過という別軸でもう一段保護する
# （Issue #63）。`--min-age-minutes 0` で無効化できる。
DEFAULT_MIN_AGE_MINUTES = 10

# `--min-age-minutes` に許す上限（分）。1週間 = 10080分。上限を設けない場合、
# 巨大な値を渡すと `timedelta` の計算自体は成立するものの、意図が読み取れない
# 設定を無警告で受け付けてしまうため、現実的な範囲に制限する（Issue #63）。
MAX_MIN_AGE_MINUTES = 10080

# DB 名に埋め込まれた PID を信用してよい期間（時間）。Linux の PID は循環する
# （`pid_max` 到達で再利用される）ため、無関係な長命プロセスが同じ PID を
# 再利用すると、本来消すべき孤児 DB が PID 生存保護によって恒久的に保護され続け、
# Issue #51 が解決した「DB が際限なく増える」問題が別経路で再発しうる。
# 24 時間走り続ける pytest は無い前提のため、作成から `PID_TRUST_WINDOW_HOURS`
# 時間を超えた DB は、DB 名の PID が生存していてもこの層の保護をスキップし、
# 他の保護層（作成直後の猶予期間・接続の有無）の判定へ進む（Issue #63）。
PID_TRUST_WINDOW_HOURS = 24

# `--apply` 時に一度に削除してよい件数の上限（サーキットブレーカー、Issue #63）。
# 「候補が既存 DB の大半を占めたら中止」という割合ベースの判定は、削除済み
# worktree の DB が積み上がった正常な状況でも発火してしまうため採用せず、
# 絶対件数の上限にすることで想定外の大量削除だけを機械的に検出する。
# `--max-delete 0` で無効化（上限なし）できる。
DEFAULT_MAX_DELETE = 10


class WorktreeDiscoveryError(RuntimeError):
    """生存 worktree の一覧取得に失敗したことを表す。

    git が使えない・リポジトリ外から実行された等が原因で、生存 worktree の
    範囲が確定できない状態。安全側に倒し、この例外を受けた呼び出し側は
    何も削除せずに終了する。
    """


class TooManyDeletionsError(RuntimeError):
    """`--apply` 時の削除候補件数が上限（`--max-delete`）を超えたことを表す。

    絶対件数によるサーキットブレーカー（Issue #63）。この例外を受けた呼び出し側
    （`main`）は、削除を一切実行せずに終了する。
    """


@dataclass(frozen=True)
class CleanupPlan:
    """1 回の実行で何をしたか / するかをまとめたレポート。"""

    live_worktree_paths: list[Path]
    existing_database_count: int
    candidates: list[str]
    to_delete: list[str]
    protected_by_connection: list[str]
    protected_by_alive_pid: list[str]
    protected_by_recent_creation: list[str]
    # `_database_creation_times` が作成時刻を取得できたか。`False` の場合、作成直後
    # DB の猶予期間保護と PID 信用期限の判定が無効化され、削除候補になった DB は
    # 従来どおり PID 生存の有無だけで安全側に保護される（Issue #63）。既存の
    # 呼び出し元（テストの `_plan_limited_to` 等）を壊さないよう既定値を持たせる。
    creation_time_protection_available: bool = True


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

    このスクリプトは必ずリポジトリ内（いずれかの worktree）から実行されるため、
    自分自身（`BACKEND_ROOT.parent`）は取得した一覧に必ず含まれるはずである。
    含まれない場合は `git worktree list` の結果が実態を反映していない状態
    （壊れた `.git/worktrees` メタデータ等）であり、他の worktree の判定も
    信用できない。そのまま処理を進めるとほぼ全ての DB が削除候補に挙がって
    しまうため、`WorktreeDiscoveryError` を送出して呼び出し側（`main`）に
    「何も削除せず終了する」経路を辿らせる（Issue #63）。
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

    live_paths = [
        path
        for path, prunable in _parse_worktree_porcelain(completed.stdout)
        if not prunable and path.exists()
    ]

    own_worktree_root = BACKEND_ROOT.parent.resolve()
    if own_worktree_root not in live_paths:
        # パス表記のズレ（シンボリックリンク経由の起動等）が原因のときに切り分け
        # られるよう、比較した自分のパスと取得できた一覧の両方をメッセージに出す
        # （Issue #63 self review）。
        live_paths_repr = ", ".join(str(path) for path in live_paths) if live_paths else "(空)"
        message = (
            "自分自身のworktree"
            f"（{own_worktree_root}）がgit worktree listの結果に含まれていません。"
            f"取得できた生存worktree一覧: [{live_paths_repr}]。"
            "git worktree listの結果が信用できない状態のため、何も削除せずに終了します。"
        )
        raise WorktreeDiscoveryError(message)

    return live_paths


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


def _as_aware_utc(value: datetime) -> datetime:
    """naive な `datetime` を UTC 前提の aware `datetime` として扱う。

    `pg_stat_file` が返す `modification` は通常 tz 付きだが、ドライバ構成
    （接続文字列のオプションや psycopg のバージョン等）によっては tz を持たない
    値が返る可能性がある。naive のまま `datetime.now(UTC)` との差分を取ると
    `TypeError` になるため、tz 情報が無ければ UTC とみなして補う（Issue #63
    self review）。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _database_creation_times(connection: Connection) -> dict[str, datetime] | None:
    """このリポジトリのテスト用 DB について、作成時刻の代わりに使える時刻を一括取得する。

    `pg_database` 自体には作成時刻を持つ列が無いため、DB ディレクトリ内の
    `PG_VERSION` ファイル（DB 作成時に一度だけ書かれ、以後更新されない）の
    更新時刻を `pg_stat_file` で読み、作成時刻の代替として使う。ローカルの
    接続ユーザーが superuser であることは実測で確認済み。

    `pg_stat_file` の第 2 引数（`missing_ok`）に `true` を渡し、対象ファイルが
    存在しない場合は例外ではなく `NULL` を返すようにする（PostgreSQL 17.10 で
    動作確認済み）。これにより、1 件のファイル欠落がクエリ全体を失敗させて
    「全 DB の保護を無効化する」事態を避けられる（Issue #63 self review）。
    `modification` が `NULL` の行（＝作成時刻が取得できなかった DB）は辞書に
    含めない。呼び出し側はそのような DB を「作成時刻不明」として扱う。

    DB 単位で毎回クエリを発行せず一括で取得するのは、呼び出し回数を減らす
    ためだけでなく、「一部の DB だけ取得に失敗する」というあいまいな状態を
    作らないため。クエリの発行自体が失敗した場合（権限不足・接続断等）は例外を
    投げっぱなしにせず、保護そのものを無効化する（`None` を返す）。呼び出し側
    が警告を出したうえで、作成時刻に基づく保護をスキップする（Issue #63）。
    """
    try:
        result = connection.execute(
            text(
                "SELECT datname, "
                "(pg_stat_file('base/' || oid || '/PG_VERSION', true)).modification "
                "FROM pg_database WHERE datname LIKE :pattern"
            ),
            {"pattern": f"{DATABASE_NAME_PREFIX}%"},
        )
        return {row[0]: _as_aware_utc(row[1]) for row in result if row[1] is not None}
    except SQLAlchemyError as exc:
        # 捕まえる範囲は DB とのやり取りの失敗に限る。`Exception` 全体を捕まえると、
        # 想定外のバグまで「保護を無効化しました」の一言で覆い隠してしまう。
        print(
            "[cleanup-test-databases][WARN] 作成時刻の取得に失敗したため、"
            f"作成直後DBの保護を無効化します: {exc}",
            file=sys.stderr,
        )
        return None


def _build_plan(
    connection: Connection,
    live_worktree_paths: list[Path],
    *,
    min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES,
) -> CleanupPlan:
    """現状の DB 一覧と生存 worktree から、削除候補と実際に削除してよい DB を仕分ける。

    削除候補（`find_database_names_without_live_worktree` の結果）を、以下の
    優先順位で保護する（いずれにも該当しない DB だけが `to_delete` に入る）。

    1. 新形式（`techradar_test_<hash8>_<pid>`）の DB で、作成から
       `PID_TRUST_WINDOW_HOURS` 時間以内かつ PID が生存している
       （別セッションの pytest が今まさに使っている可能性が高いため、最優先で保護。
       作成時刻が不明な場合は従来どおり PID 生存だけで安全側に保護する）
    2. 作成から `min_age_minutes` 分未満（`min_age_minutes` が 0 なら無効）
    3. 接続が残っている（他プロセスが使用中の可能性を否定できない）

    旧形式（PID 接尾辞なし）の DB は PID を持たないため 1. の対象外。

    Raises:
        ValueError: `min_age_minutes` が負の場合。CLI 層（`main`）の
            `parser.error` と二重に防御する（Issue #63）。
    """
    if min_age_minutes < 0:
        message = f"min_age_minutesは0以上を指定してください（実際: {min_age_minutes}）。"
        raise ValueError(message)

    existing_names = _existing_test_database_names(connection)
    live_backend_roots = [path / "backend" for path in live_worktree_paths]
    candidates = find_database_names_without_live_worktree(
        existing_names,
        live_backend_roots=live_backend_roots,
    )

    # PID 信用期限（1.）は `min_age_minutes` の値に関わらず常に必要なため、猶予期間
    # 保護（2.）が無効化されていても作成時刻は取得する（Issue #63）。
    creation_times = _database_creation_times(connection)
    creation_time_protection_available = creation_times is not None
    age_threshold = timedelta(minutes=min_age_minutes)
    pid_trust_window = timedelta(hours=PID_TRUST_WINDOW_HOURS)
    now = datetime.now(UTC)

    to_delete: list[str] = []
    protected_by_connection: list[str] = []
    protected_by_alive_pid: list[str] = []
    protected_by_recent_creation: list[str] = []
    for name in candidates:
        parsed = parse_database_name(name)
        created_at = creation_times.get(name) if creation_times is not None else None

        if parsed is not None:
            _, pid = parsed
            # 作成時刻が分かっていて、かつ信用期限を過ぎている場合だけ PID 生存保護を
            # スキップする。作成時刻不明の場合は従来どおり安全側（PID生存を信用）。
            pid_trust_expired = created_at is not None and (now - created_at) >= pid_trust_window
            if not pid_trust_expired and pid_is_alive(pid):
                protected_by_alive_pid.append(name)
                continue

        if created_at is not None and (now - created_at) < age_threshold:
            protected_by_recent_creation.append(name)
            continue

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
        protected_by_alive_pid=protected_by_alive_pid,
        protected_by_recent_creation=protected_by_recent_creation,
        creation_time_protection_available=creation_time_protection_available,
    )


def _apply_plan(connection: Connection, plan: CleanupPlan) -> CleanupPlan:
    """`plan.to_delete` を実際に DROP し、結果を反映した新しい `CleanupPlan` を返す。

    `_build_plan` が接続の有無や PID の生死を確認してから、この関数が実際に DROP
    するまでにはレポート表示等の時間差があるが、`_build_plan` から `_apply_plan`
    までは同一プロセス内の連続実行であり、この窓（通常ミリ秒〜数秒）で DB 名に
    埋め込まれた PID が再利用される確率は無視できる。一方、接続については話が別で、
    `_build_plan` の確認後に新しい接続が張られることは普通に起こりうるため、DROP の
    直前に `_has_active_connections` を再確認する意味は大きい。`pid_is_alive` の
    再確認は、確率的にはほぼ意味を持たないが、`_has_active_connections` と対称な
    作りにして判定経路を単純に保つために揃えている（正直に言えば、省略しても実害は
    ほぼ無い）。DROP 直前の再確認で該当した DB は削除せず、対応する保護区分
    （`protected_by_connection` / `protected_by_alive_pid`）へ回して（既存の DB と
    同じ理由のため合流させる）レポートに反映されるようにする。作成からの経過時間
    （`protected_by_recent_creation`）は `_build_plan` から DROP までの間に短縮
    されることが無い（時間は不可逆に進む一方のため）ので、ここでは再確認しない。

    `CleanupPlan` は不変（frozen dataclass）のため、既存のインスタンスを書き換えず
    新しいインスタンスを作って返す。
    """
    dropped: list[str] = []
    skipped_at_drop_connection: list[str] = []
    skipped_at_drop_alive_pid: list[str] = []
    for name in plan.to_delete:
        parsed = parse_database_name(name)
        if parsed is not None:
            _, pid = parsed
            if pid_is_alive(pid):
                skipped_at_drop_alive_pid.append(name)
                continue
        if _has_active_connections(connection, name):
            skipped_at_drop_connection.append(name)
            continue
        _drop_database(connection, name)
        dropped.append(name)

    if not skipped_at_drop_connection and not skipped_at_drop_alive_pid:
        return plan

    return CleanupPlan(
        live_worktree_paths=plan.live_worktree_paths,
        existing_database_count=plan.existing_database_count,
        candidates=plan.candidates,
        to_delete=dropped,
        protected_by_connection=[*plan.protected_by_connection, *skipped_at_drop_connection],
        protected_by_alive_pid=[*plan.protected_by_alive_pid, *skipped_at_drop_alive_pid],
        protected_by_recent_creation=plan.protected_by_recent_creation,
        creation_time_protection_available=plan.creation_time_protection_available,
    )


def _check_max_delete(plan: CleanupPlan, max_delete: int) -> None:
    """`--apply` 時のサーキットブレーカー。削除候補が `max_delete` 件を超えていたら
    `TooManyDeletionsError` を送出する（Issue #63）。

    「候補が既存 DB の大半を占めたら中止」という割合ベースの判定は、削除済み
    worktree の DB が積み上がった正常な状況でも発火してしまうため採用しない。
    絶対件数の上限にすることで、想定外の大量削除だけを機械的に検出する。
    `max_delete` に 0 以下を指定すると上限なしとして扱う。
    """
    if max_delete <= 0:
        return
    if len(plan.to_delete) <= max_delete:
        return
    message = (
        f"削除候補が{len(plan.to_delete)}件あり、上限（--max-delete {max_delete}件）を"
        "超えています。想定外に多い可能性があります。dry-run"
        "（--applyを付けずに実行）で内容を確認し、意図どおりであれば"
        "--max-delete Nを指定して再実行してください。"
    )
    raise TooManyDeletionsError(message)


def _print_report(plan: CleanupPlan, *, applied: bool, max_delete: int) -> None:
    """人が読める形で削除候補・保護対象を報告する。"""
    print(f"[cleanup-test-databases] 生存worktree: {len(plan.live_worktree_paths)}件")
    for path in plan.live_worktree_paths:
        print(f"  - {path}")

    print(f"[cleanup-test-databases] 既存のテスト用DB: {plan.existing_database_count}件")
    print(
        f"[cleanup-test-databases] 削除候補（生存worktreeに属さないDB）: {len(plan.candidates)}件"
    )

    if not plan.creation_time_protection_available:
        # 無人実行（skill経由等）でstderrを捨てる構成だと、_database_creation_times
        # が出す警告に気づけない。標準出力にも同じ内容を出す（Issue #63）。
        print(
            "[cleanup-test-databases][WARN] 作成直後DBの保護は無効です"
            "（作成時刻を取得できませんでした）。PID信用期限の判定も安全側"
            "（PID生存を信用する側）に倒れています。"
        )

    if plan.to_delete:
        action_label = "削除しました" if applied else "削除対象（--applyで削除）"
        print(f"[cleanup-test-databases] {action_label}: {len(plan.to_delete)}件")
        for name in plan.to_delete:
            print(f"  - {name}")
    else:
        print("[cleanup-test-databases] 削除対象なし")

    if plan.protected_by_alive_pid:
        print(
            "[cleanup-test-databases] 保護（DB名に埋め込まれたPIDが生存しているため削除しない）: "
            f"{len(plan.protected_by_alive_pid)}件"
        )
        for name in plan.protected_by_alive_pid:
            print(f"  - {name}")

    if plan.protected_by_recent_creation:
        print(
            "[cleanup-test-databases] 保護（作成から間もないため削除しない）: "
            f"{len(plan.protected_by_recent_creation)}件"
        )
        for name in plan.protected_by_recent_creation:
            print(f"  - {name}")

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
        if max_delete > 0 and len(plan.to_delete) > max_delete:
            # dry-runでは中止せず候補を全て表示したうえで警告するに留める
            # （人が全体を見られるようにするため）。実際に中止するのは--apply時
            # （_check_max_delete）。
            print(
                "[cleanup-test-databases][WARN] 削除候補が"
                f"{len(plan.to_delete)}件あり、--max-delete（{max_delete}件）を"
                "超えています。--apply時はこのままでは中止されます。内容を確認し、"
                "意図どおりであれば--max-delete Nを指定してください。"
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
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=DEFAULT_MIN_AGE_MINUTES,
        help=(
            "作成からこの分数未満のDBを削除対象から保護する猶予期間"
            f"（既定{DEFAULT_MIN_AGE_MINUTES}分）。0を指定すると無効化する。"
            f"上限は{MAX_MIN_AGE_MINUTES}分（1週間）。"
        ),
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=DEFAULT_MAX_DELETE,
        help=(
            "--apply時に一度に削除してよい件数の上限（サーキットブレーカー、既定"
            f"{DEFAULT_MAX_DELETE}件）。超えた場合は何も削除せずエラー終了する。"
            "0を指定すると上限なし。"
        ),
    )
    args = parser.parse_args(argv)
    if args.min_age_minutes < 0:
        parser.error("--min-age-minutesは0以上を指定してください。")
    if args.min_age_minutes > MAX_MIN_AGE_MINUTES:
        # `timedelta` 自体はもっと大きな値も扱えるが、意図が読み取れない設定を
        # 無警告で受け付けないよう現実的な範囲に制限する（Issue #63 self review）。
        parser.error(f"--min-age-minutesは{MAX_MIN_AGE_MINUTES}（1週間）以下を指定してください。")
    if args.max_delete < 0:
        parser.error("--max-deleteは0以上を指定してください。")

    try:
        live_worktree_paths = _discover_live_worktree_paths()
    except WorktreeDiscoveryError as exc:
        print(f"[cleanup-test-databases][ERROR] {exc}", file=sys.stderr)
        print("[cleanup-test-databases][ERROR] 何も削除せずに終了します。", file=sys.stderr)
        return 1

    admin_engine = _admin_engine()
    try:
        with admin_engine.connect() as connection:
            plan = _build_plan(
                connection, live_worktree_paths, min_age_minutes=args.min_age_minutes
            )
            if args.apply:
                try:
                    _check_max_delete(plan, args.max_delete)
                except TooManyDeletionsError as exc:
                    print(f"[cleanup-test-databases][ERROR] {exc}", file=sys.stderr)
                    return 1
                plan = _apply_plan(connection, plan)
            _print_report(plan, applied=args.apply, max_delete=args.max_delete)
    finally:
        admin_engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
