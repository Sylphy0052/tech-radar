"""掃除ロジックのテストが使うダミー worktree パスを組み立てる（Issue #59）。

`tests/conftest.py` と `scripts/cleanup_test_databases.py` の掃除ロジックは、
worktree のパスから決まるハッシュで「自分の DB かどうか」を判定する。これらを
テストするには、実 worktree とは別のハッシュになるダミーのパスが要る。

ダミーのパスは実行中の worktree と pytest プロセスから派生させる。固定値にすると
どの worktree のどのプロセスから実行しても同じ DB 名になり、複数の pytest が同時に
走ったときに `DuplicateDatabase` で落ちる（Issue #23 の worktree 単位分離と
Issue #33 のプロセス単位分離が、このダミーにだけ効いていなかった）。
"""

from __future__ import annotations

from pathlib import Path

# 実プロセスをフォーク/待機させずに「確実に死んでいる PID」を得るための値
# （32bit 符号付き pid_t の上限付近。OS 上に実在しえない）。ダミーのパスが
# プロセスごとに分かれるため、この値は固定のままでよい。
DEAD_PID = 2147483647
ANOTHER_DEAD_PID = 2147483646


def fake_worktree_path(base: Path, label: str, pid: int) -> Path:
    """`base` の兄弟にあたる、実在しないダミーパスを返す。

    `base` に実 worktree のパスを渡しても、末尾へラベルと PID を足すためハッシュは
    実 worktree のものと別値になる。掃除ロジックはパス文字列からハッシュを取るだけで
    ファイルシステムを見ないため、実在しないパスでも判定は成立する。
    """
    return base.parent / f"{base.name}-test-fixture-{label}-{pid}"
