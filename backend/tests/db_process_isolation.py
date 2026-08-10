"""pytest 用テストDBをプロセス単位に分離するための純粋ロジック（Issue #33）。

同じ worktree で pytest を複数プロセス同時実行すると、`tests/conftest.py` の
セッション開始時 DROP/CREATE が同じ DB 名を取り合い、互いのテストデータを
破壊してしまう（Issue #23 で導入した worktree 単位の分離だけでは足りない）。
DB 名に呼び出し元プロセスの PID を付与してプロセス単位に分離し、異常終了で
残った孤児 DB は次回のセッション開始時に掃除する。加えて、この分離導入前
（Issue #23 時代）に作られた PID 接尾辞なしの旧形式 DB（`techradar_test_<hash8>`）
も、誰にも使われなくなった分は同様に掃除対象とする。

このモジュールは「DB 名の組み立て」「孤児 DB / 旧形式 DB の判定」という
副作用の無いロジックだけを置く。実際の DB 操作（DROP/CREATE、PID の生存確認、
接続数の確認）は `tests/conftest.py` 側が担う。`test_` プレフィックスを
付けていないため pytest には収集されない（同様の前例は `tests/schema_parity.py`。
このモジュール自体のテストは `tests/test_db_process_isolation.py` に置く）。

PID 付き孤児（`find_orphaned_database_names`）と PID 無し旧形式
（`find_own_worktree_legacy_database_names`）は判定条件が異なるため別関数に
分けている。前者は「PID が死んでいること」を生存シグナルにできるが、後者は
PID を持たないため worktree ハッシュの一致までしか判定できず、最終的な削除可否
（接続が残っていないか）は呼び出し側が別途確認する。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from pathlib import Path

# テスト用 DB 名の接頭辞。孤児掃除の際に既存 DB 一覧から絞り込むのにも使う。
DATABASE_NAME_PREFIX = "techradar_test_"

# worktree を識別するハッシュ接尾辞の長さ（Issue #23 から踏襲）。
# DB 名の上限（63 バイト）に収めつつ、worktree 数十個程度で衝突しない長さにする。
WORKTREE_HASH_LENGTH = 8

# PostgreSQL の識別子（データベース名を含む）は 63 バイトを超えると自動的に
# 切り詰められる。切り詰めによる別名同士の衝突を避けるため、組み立て時点で検出する。
POSTGRES_IDENTIFIER_MAX_BYTES = 63

# DB 名の PID 部分に許容する最大桁数。Linux の `pid_max` の既定値は 4194304
# （7 桁）だが、将来 `pid_max` が拡張される余地を見て 10 桁まで許容する。
# 桁数の上限を設けない場合、`techradar_test_<hash8>_999999999999999999999999`
# のような名前が「このリポジトリが生成した名前」として通過してしまい、
# `os.kill()` へ渡した際に `OverflowError` を誘発しうる（Issue #33 self review）。
PID_DIGITS_MAX = 10

# `techradar_test_<hash8>_<pid>` 形式。pid 部分は 1〜`PID_DIGITS_MAX` 桁の数字のみを
# 許可し、それ以外（空・非数字を含む・桁数超過等）は「このリポジトリが生成した
# 名前ではない」として扱う（後続の孤児判定で安全側＝消さない側に倒すため）。
# 終端は `$` ではなく `\Z` を使う。`re` の `$` は文字列末尾の直前の改行にも
# マッチするため、末尾に改行を含む名前を誤って通過させうる（Issue #33 self review）。
_DATABASE_NAME_PATTERN = re.compile(
    rf"^{re.escape(DATABASE_NAME_PREFIX)}"
    rf"(?P<hash>[0-9a-f]{{{WORKTREE_HASH_LENGTH}}})_(?P<pid>[0-9]{{1,{PID_DIGITS_MAX}}})\Z"
)

# `techradar_test_<hash8>` 形式（PID 接尾辞なし、Issue #23 時代の旧形式）。
# ハッシュ部分の長さ・文字種が厳密に一致しない名前は対象外とする
# （切り詰められた名前や無関係な DB を誤って旧形式扱いしないため）。
# 終端を `\Z` にする理由は `_DATABASE_NAME_PATTERN` と同じ。
_LEGACY_DATABASE_NAME_PATTERN = re.compile(
    rf"^{re.escape(DATABASE_NAME_PREFIX)}(?P<hash>[0-9a-f]{{{WORKTREE_HASH_LENGTH}}})\Z"
)


def worktree_hash(backend_root: Path) -> str:
    """`backend_root`（worktree のパス）から決まるハッシュ接尾辞を返す。

    同じ worktree では常に同じ値になり、別 worktree とは（ほぼ）衝突しない。
    """
    digest = hashlib.blake2s(str(backend_root).encode("utf-8")).hexdigest()
    return digest[:WORKTREE_HASH_LENGTH]


def build_database_name(backend_root: Path, pid: int) -> str:
    """`backend_root` と `pid` から、このプロセス専用のテスト用 DB 名を組み立てる。

    同一 worktree で pytest を複数プロセス同時実行しても、プロセスごとに
    別々の DB 名になるため、セッション開始時の DROP/CREATE が互いに干渉しない。
    """
    name = f"{DATABASE_NAME_PREFIX}{worktree_hash(backend_root)}_{pid}"
    name_bytes = len(name.encode("utf-8"))
    if name_bytes > POSTGRES_IDENTIFIER_MAX_BYTES:
        message = (
            f"テスト用 DB 名が PostgreSQL の識別子上限（{POSTGRES_IDENTIFIER_MAX_BYTES} バイト）"
            f"を超えています（{name_bytes} バイト）: {name}"
        )
        raise ValueError(message)
    return name


def parse_database_name(database_name: str) -> tuple[str, int] | None:
    """テスト用 DB 名から (worktree ハッシュ, PID) を取り出す。

    このモジュールが生成した形式（`techradar_test_<hash8>_<pid>`）に一致しない
    場合は `None` を返す。pid 部分が数字でない・欠けている等、解釈できない名前は
    すべてここで弾かれる。
    """
    match = _DATABASE_NAME_PATTERN.match(database_name)
    if match is None:
        return None
    return match.group("hash"), int(match.group("pid"))


def find_orphaned_database_names(
    existing_names: Iterable[str],
    *,
    backend_root: Path,
    own_pid: int,
    is_pid_alive: Callable[[int], bool],
) -> list[str]:
    """`existing_names` のうち、同じ worktree に属する孤児テスト用 DB の名前を返す。

    孤児 = 異常終了した過去の pytest プロセスが後始末できずに残した DB。
    以下のいずれかに該当する DB は孤児と判定しない（安全側＝消さない側に倒す）:

    - 名前が `techradar_test_<hash8>_<pid>` の形式に一致しない（PID 部分が解釈できない）
    - 別 worktree のハッシュを持つ（自分たちの worktree の DB ではない）
    - 自分自身（`own_pid`）の DB
    - `is_pid_alive(pid)` が真、つまりそのプロセスがまだ生きている

    DB への実接続が必要な確認（「その DB に接続が残っているか」）はここでは行わない。
    呼び出し側（`conftest.py`）が実際に DROP する直前に別途確認すること。
    """
    own_hash = worktree_hash(backend_root)
    orphans: list[str] = []
    for name in existing_names:
        parsed = parse_database_name(name)
        if parsed is None:
            continue
        db_hash, pid = parsed
        if db_hash != own_hash:
            continue
        if pid == own_pid:
            continue
        if is_pid_alive(pid):
            continue
        orphans.append(name)
    return orphans


def parse_legacy_database_name(database_name: str) -> str | None:
    """PID 接尾辞なし旧形式（`techradar_test_<hash8>`、Issue #23 時代）の
    DB 名から worktree ハッシュを取り出す。

    現行形式（`techradar_test_<hash8>_<pid>`）はこの関数の対象外（`None`）。
    ハッシュ部分の長さ・文字種が一致しない名前も対象外。
    """
    match = _LEGACY_DATABASE_NAME_PATTERN.match(database_name)
    if match is None:
        return None
    return match.group("hash")


def find_own_worktree_legacy_database_names(
    existing_names: Iterable[str],
    *,
    backend_root: Path,
) -> list[str]:
    """`existing_names` のうち、自分の worktree に属する旧形式 DB の名前を返す。

    旧形式（`techradar_test_<hash8>`、Issue #23 時代）は PID を持たないため、
    `find_orphaned_database_names` のような生存プロセス判定ができない。
    そのため、この関数は「掃除してよいかもしれない候補」を worktree ハッシュの
    一致だけで絞り込むところまでに留める。実際に削除してよいかの最終判断
    （その DB への接続が残っていないこと＝唯一の生存シグナル）は、呼び出し側
    （`conftest.py`）が DROP する直前に別途確認すること。

    別 worktree のハッシュを持つ DB は、そちらの worktree でまだ旧コードの
    pytest が走る可能性があるため、ここでは絶対に候補に含めない。
    """
    own_hash = worktree_hash(backend_root)
    return [name for name in existing_names if parse_legacy_database_name(name) == own_hash]
