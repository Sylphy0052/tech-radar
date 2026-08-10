"""`tests/conftest.py` の `_pid_is_alive` のテスト（Issue #33 self review 対応）。

DB 名の PID 部分の桁数には上限（`db_process_isolation.PID_DIGITS_MAX`）を
設けたため、通常の掃除経路（`_cleanup_orphaned_test_databases`）を通る限り
`_pid_is_alive` へ巨大な PID が渡ることは無い。ただし `_pid_is_alive` 自体は
どこから呼ばれても安全であるべき独立した安全弁なので、直接テストする。

`os.kill(pid, 0)` へ極端に大きい PID を渡すと `OverflowError` が飛ぶ
（`ProcessLookupError` を含む `OSError` のサブクラスではない）。修正前は
これが未処理例外としてそのまま伝播し、孤児 DB 掃除全体（延いては pytest
セッションの起動）を落としていた。実DBへの接続は不要な純粋な単体テスト。
"""

from __future__ import annotations

import os

from tests.conftest import _pid_is_alive

# 32bit 符号付き `pid_t` の範囲を超える極端に大きい PID。`os.kill()` へ渡すと
# `ProcessLookupError` ではなく `OverflowError` が飛ぶことを
# `python3 -c "import os; os.kill(10**30, 0)"` で確認済み。
_HUGE_PID_TRIGGERING_OVERFLOW_ERROR = 10**30

# OS 上に実在しえない極端に大きい PID（32bit 符号付き pid_t の上限付近）。
# `OverflowError` は起こさず、`ProcessLookupError`（存在しない）だけを誘発する。
# 実プロセスをフォーク/待機させずに「確実に死んでいる PID」を得るために使う。
_DEAD_BUT_VALID_PID = 2147483647


def test_returns_true_for_pid_that_triggers_overflow_error() -> None:
    """`OverflowError`（`ProcessLookupError`以外の異常）は安全側＝生存扱いに倒す。"""
    # Act / Assert — 例外を伝播させず、安全側（生存扱い）の True を返す
    assert _pid_is_alive(_HUGE_PID_TRIGGERING_OVERFLOW_ERROR) is True


def test_returns_false_for_a_pid_that_does_not_exist() -> None:
    """実在しない PID には偽を返す（`ProcessLookupError` → 生存していない判定）。"""
    # Act / Assert
    assert _pid_is_alive(_DEAD_BUT_VALID_PID) is False


def test_returns_true_for_own_pid() -> None:
    """現在実行中の自プロセス自身の PID は生存している。"""
    # Act / Assert
    assert _pid_is_alive(os.getpid()) is True
