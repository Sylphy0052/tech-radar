"""`tests/fake_worktree_roots` の振る舞いを検証する（Issue #59）。

このモジュールが返すダミーパスは、掃除ロジックのテストが `DROP DATABASE` の対象を
決める名前空間そのものになる。性質が崩れると、テストが他 worktree や他プロセスの
テスト用 DB を巻き込む・取り合うといった形で表に出る。消費側のテストではなくここで
まとめて検証する。
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.db_process_isolation import pid_is_alive, worktree_hash
from tests.fake_worktree_roots import ANOTHER_DEAD_PID, DEAD_PID, fake_worktree_path


class TestFakeWorktreePath:
    """`fake_worktree_path` が名前空間を分ける性質。"""

    def test_derives_a_distinct_path_per_worktree_process_and_label(self) -> None:
        # Arrange
        base = Path("/somewhere/techradar/backend")
        other_base = Path("/elsewhere/techradar/backend")

        # Act / Assert — worktree・プロセス・ラベルのどれが違っても別パスになること
        assert fake_worktree_path(base, "a", 100) != fake_worktree_path(other_base, "a", 100)
        assert fake_worktree_path(base, "a", 100) != fake_worktree_path(base, "a", 200)
        assert fake_worktree_path(base, "a", 100) != fake_worktree_path(base, "b", 100)

    def test_returns_a_sibling_of_the_given_path(self) -> None:
        # Arrange
        base = Path("/somewhere/techradar/backend")

        # Act
        result = fake_worktree_path(base, "a", 100)

        # Assert — 掃除ロジックが見る「自分のハッシュ」と別値になるよう、末尾だけを変える
        assert result.parent == base.parent
        assert result.name != base.name
        assert base.name in result.name

    def test_does_not_share_a_hash_with_the_given_path(self) -> None:
        # Arrange
        base = Path("/somewhere/techradar/backend")

        # Act / Assert — 実 worktree のテスト用 DB を巻き込まないこと
        assert worktree_hash(fake_worktree_path(base, "a", 100)) != worktree_hash(base)


class TestDeadPids:
    """ダミー PID が「確実に死んでいる」こと。"""

    def test_are_out_of_the_range_the_os_assigns(self) -> None:
        # Arrange / Act / Assert — 実プロセスと衝突しない値であること
        # （Linux の `pid_max` は既定で 4194304、上限でも 32bit 符号付きの最大値）
        assert DEAD_PID != ANOTHER_DEAD_PID
        for pid in (DEAD_PID, ANOTHER_DEAD_PID):
            assert pid > 4194304
            assert pid != os.getpid()

    def test_are_not_alive(self) -> None:
        # Arrange / Act / Assert — 掃除ロジックが使う生存判定に掛けても死んでいると出ること
        for pid in (DEAD_PID, ANOTHER_DEAD_PID):
            assert pid_is_alive(pid) is False
