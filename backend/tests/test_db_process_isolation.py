"""`tests/db_process_isolation.py` のテスト（Issue #33）。

同じ worktree で pytest を複数プロセス同時実行したときに、テスト用 DB の
DROP/CREATE が互いに干渉しないための「DB 名の組み立て」「孤児 DB 判定」を
実 DB を使わずに検証する。実際の DB 操作（DROP/CREATE、PID 生存確認）は
`tests/conftest.py` 側の責務であり、ここでは扱わない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.db_process_isolation import (
    PID_DIGITS_MAX,
    POSTGRES_IDENTIFIER_MAX_BYTES,
    build_database_name,
    find_database_names_without_live_worktree,
    find_orphaned_database_names,
    find_own_worktree_legacy_database_names,
    parse_database_name,
    parse_legacy_database_name,
    worktree_hash,
)

_BACKEND_ROOT = Path("/home/example/workspace/techradar")
_OTHER_BACKEND_ROOT = Path("/home/example/workspace/techradar-other-worktree")
_REMOVED_BACKEND_ROOT = Path("/home/example/workspace/techradar-removed-worktree")


class TestBuildDatabaseName:
    def test_has_expected_shape_and_fits_postgres_limit(self):
        # Arrange
        pid = 12345

        # Act
        name = build_database_name(_BACKEND_ROOT, pid)

        # Assert
        expected_hash = worktree_hash(_BACKEND_ROOT)
        assert name == f"techradar_test_{expected_hash}_{pid}"
        assert len(name.encode("utf-8")) <= POSTGRES_IDENTIFIER_MAX_BYTES

    def test_differs_by_pid(self):
        # Arrange / Act
        first = build_database_name(_BACKEND_ROOT, 111)
        second = build_database_name(_BACKEND_ROOT, 222)

        # Assert
        assert first != second

    def test_differs_by_worktree(self):
        # Arrange / Act
        first = build_database_name(_BACKEND_ROOT, 111)
        second = build_database_name(_OTHER_BACKEND_ROOT, 111)

        # Assert — 同じ PID でも worktree が違えば別名になる（Issue #23 の性質を保つ）
        assert first != second


class TestParseDatabaseName:
    def test_round_trips_with_build_database_name(self):
        # Arrange
        name = build_database_name(_BACKEND_ROOT, 999)

        # Act
        parsed = parse_database_name(name)

        # Assert
        assert parsed == (worktree_hash(_BACKEND_ROOT), 999)

    @pytest.mark.parametrize(
        "database_name",
        [
            "techradar_test_deadbeef_notapid",  # PID 部分が数字でない
            "techradar_test_deadbeef_",  # PID 部分が空
            "techradar_test_deadbeef",  # PID 部分が無い（Issue #23 時点の旧形式）
            "some_other_database",  # 無関係な DB 名
            "postgres",  # 管理用 DB
            "techradar_test_deadbeef_" + "9" * (PID_DIGITS_MAX + 1),  # PID 桁数が上限超過
            "techradar_test_deadbeef_123\n",  # 末尾に改行（`$`ではなく`\Z`終端であることの確認）
        ],
    )
    def test_returns_none_for_unrecognized_formats(self, database_name: str):
        # Act / Assert
        assert parse_database_name(database_name) is None

    def test_returns_none_for_pid_exceeding_digit_limit_even_when_byte_length_fits(self):
        """`build_database_name` はバイト長（63バイト上限）しか検証しないため、
        PID の桁数が `PID_DIGITS_MAX` を超えていても組み立て自体は成功しうる
        （例: 11桁の PID でも `techradar_test_<hash8>_<pid>` は63バイトに収まる）。
        そのような名前を `parse_database_name` が解釈できないこと（＝孤児掃除の
        対象から外れ、消さない側に倒れること）を確認する（Issue #33 self review）。
        """
        # Arrange — 桁数上限（10桁）を1桁超える PID
        oversized_pid = 10**PID_DIGITS_MAX  # 11桁
        name = build_database_name(_BACKEND_ROOT, oversized_pid)

        # Act / Assert
        assert parse_database_name(name) is None

    def test_accepts_pid_at_the_digit_limit_boundary(self):
        """桁数上限ちょうど（`PID_DIGITS_MAX`桁）の PID は正しく解釈できる境界値確認。"""
        # Arrange
        boundary_pid = (10**PID_DIGITS_MAX) - 1  # 10桁のうち最大値
        name = build_database_name(_BACKEND_ROOT, boundary_pid)

        # Act
        parsed = parse_database_name(name)

        # Assert
        assert parsed == (worktree_hash(_BACKEND_ROOT), boundary_pid)


class TestFindOrphanedDatabaseNames:
    def test_returns_only_dead_pid_databases_of_own_worktree(self):
        # Arrange — 死んだ PID / 生存中 PID / 自分自身 / 解釈不能 / 別 worktree を混在させる
        own_pid = 100
        dead_pid_name = build_database_name(_BACKEND_ROOT, 200)
        alive_pid_name = build_database_name(_BACKEND_ROOT, 300)
        own_name = build_database_name(_BACKEND_ROOT, own_pid)
        unparsable_name = "techradar_test_deadbeef_notapid"
        other_worktree_name = build_database_name(_OTHER_BACKEND_ROOT, 200)
        existing_names = [
            dead_pid_name,
            alive_pid_name,
            own_name,
            unparsable_name,
            other_worktree_name,
        ]

        def is_pid_alive(pid: int) -> bool:
            return pid == 300  # 300 のみ生存している想定

        # Act
        orphans = find_orphaned_database_names(
            existing_names,
            backend_root=_BACKEND_ROOT,
            own_pid=own_pid,
            is_pid_alive=is_pid_alive,
        )

        # Assert — 死んだ PID かつ自分の worktree の DB だけが孤児判定される
        assert orphans == [dead_pid_name]

    def test_returns_empty_list_when_no_orphans(self):
        # Arrange
        existing_names = [build_database_name(_BACKEND_ROOT, 100)]

        # Act
        orphans = find_orphaned_database_names(
            existing_names,
            backend_root=_BACKEND_ROOT,
            own_pid=100,
            is_pid_alive=lambda _pid: True,
        )

        # Assert
        assert orphans == []

    def test_pid_reuse_after_own_process_treats_new_owner_as_alive(self):
        """PID 再利用時の安全側動作: `is_pid_alive` がプロセス生存を正しく報告する限り、
        再利用された PID の DB は「生存中」として孤児判定されない。
        """
        # Arrange — かつて存在したプロセスの DB だが、同じ PID が別プロセスに再利用され
        # 現在は生きている、という状況を模す。
        reused_pid = 400
        reused_pid_name = build_database_name(_BACKEND_ROOT, reused_pid)

        # Act
        orphans = find_orphaned_database_names(
            [reused_pid_name],
            backend_root=_BACKEND_ROOT,
            own_pid=999,
            is_pid_alive=lambda pid: pid == reused_pid,
        )

        # Assert — 生存判定が真である限り、誤って削除対象にはならない
        assert orphans == []

    def test_legacy_format_names_never_appear_as_pid_based_orphans(self):
        """PID 接尾辞なしの旧形式（Issue #23 時代）は、そもそも
        `techradar_test_<hash8>_<pid>` に一致しないため PID 付き孤児判定の
        候補にはならない（`find_own_worktree_legacy_database_names` 側の
        責務であり、ここで二重に扱わない）。
        """
        # Arrange
        legacy_name = f"techradar_test_{worktree_hash(_BACKEND_ROOT)}"

        # Act
        orphans = find_orphaned_database_names(
            [legacy_name],
            backend_root=_BACKEND_ROOT,
            own_pid=1,
            is_pid_alive=lambda _pid: False,
        )

        # Assert
        assert orphans == []


class TestFindOwnWorktreeLegacyDatabaseNames:
    """Issue #23 時代の PID 接尾辞なし旧形式 DB（`techradar_test_<hash8>`）の
    掃除候補を絞り込むロジック（Issue #33 追加分）。

    旧形式は PID を持たないため生存判定ができない。ここでは worktree ハッシュの
    一致だけを見て候補に挙げる。実際に削除してよいかの最終判断（DB への接続が
    残っていないこと）は `conftest.py` 側が DROP 直前に別途確認する。
    """

    def test_includes_legacy_database_of_own_worktree(self):
        # Arrange
        own_legacy_name = f"techradar_test_{worktree_hash(_BACKEND_ROOT)}"

        # Act
        candidates = find_own_worktree_legacy_database_names(
            [own_legacy_name],
            backend_root=_BACKEND_ROOT,
        )

        # Assert
        assert candidates == [own_legacy_name]

    def test_excludes_legacy_database_of_other_worktree(self):
        """別 worktree のハッシュを持つ旧形式 DB は絶対に候補に入れない。
        そちらの worktree ではまだ旧コードの pytest が走りうるため。
        """
        # Arrange
        other_legacy_name = f"techradar_test_{worktree_hash(_OTHER_BACKEND_ROOT)}"

        # Act
        candidates = find_own_worktree_legacy_database_names(
            [other_legacy_name],
            backend_root=_BACKEND_ROOT,
        )

        # Assert
        assert candidates == []

    @pytest.mark.parametrize(
        "database_name",
        [
            "techradar_test_deadbee",  # ハッシュ部分が1文字短い
            "techradar_test_deadbeefx",  # ハッシュ部分が1文字長い
            "techradar_test_deadbeeg",  # ハッシュ部分に16進以外の文字を含む
            "techradar_test_DEADBEEF",  # 16進として不正（大文字は blake2s hexdigest に出ない）
        ],
    )
    def test_excludes_names_with_malformed_hash_part(self, database_name: str):
        # Act
        candidates = find_own_worktree_legacy_database_names(
            [database_name],
            backend_root=_BACKEND_ROOT,
        )

        # Assert
        assert candidates == []

    def test_excludes_name_with_trailing_newline(self):
        """終端を`$`ではなく`\\Z`にしたことの確認（Issue #33 self review）。

        `re`の`$`は文字列末尾の直前の改行にもマッチするため、末尾に改行を1つ
        含む名前を誤って旧形式として通過させうる。`\\Z`ではマッチしない。
        """
        # Arrange — 自分の worktree ハッシュ + 末尾改行
        name_with_trailing_newline = f"techradar_test_{worktree_hash(_BACKEND_ROOT)}\n"

        # Act / Assert
        assert parse_legacy_database_name(name_with_trailing_newline) is None
        assert (
            find_own_worktree_legacy_database_names(
                [name_with_trailing_newline],
                backend_root=_BACKEND_ROOT,
            )
            == []
        )

    def test_excludes_pid_based_new_format_names(self):
        """PID 付きの新形式は旧形式の候補側には現れない（二重計上しない）。"""
        # Arrange
        new_format_name = build_database_name(_BACKEND_ROOT, 123)

        # Act
        candidates = find_own_worktree_legacy_database_names(
            [new_format_name],
            backend_root=_BACKEND_ROOT,
        )

        # Assert
        assert candidates == []


class TestFindDatabaseNamesWithoutLiveWorktree:
    """削除済み worktree の残骸 DB を検出するロジック（Issue #51）。

    `find_orphaned_database_names` / `find_own_worktree_legacy_database_names` は
    「worktree は存在し続ける」前提で PID やハッシュを見るが、worktree 自体が
    削除されるとそのハッシュを持つ DB はどちらの経路にも掛からず残り続ける。
    ここでは「生存 worktree の一覧」だけを根拠に、新旧どちらの形式も対象にする。
    """

    def test_excludes_new_format_database_of_live_worktree(self):
        # Arrange
        live_name = build_database_name(_BACKEND_ROOT, 111)

        # Act
        orphaned = find_database_names_without_live_worktree(
            [live_name],
            live_backend_roots=[_BACKEND_ROOT, _OTHER_BACKEND_ROOT],
        )

        # Assert
        assert orphaned == []

    def test_excludes_legacy_format_database_of_live_worktree(self):
        # Arrange
        live_legacy_name = f"techradar_test_{worktree_hash(_BACKEND_ROOT)}"

        # Act
        orphaned = find_database_names_without_live_worktree(
            [live_legacy_name],
            live_backend_roots=[_BACKEND_ROOT],
        )

        # Assert
        assert orphaned == []

    def test_includes_new_format_database_without_live_worktree(self):
        # Arrange — worktree 削除済みのハッシュを持つ新形式 DB
        removed_name = build_database_name(_REMOVED_BACKEND_ROOT, 222)

        # Act
        orphaned = find_database_names_without_live_worktree(
            [removed_name],
            live_backend_roots=[_BACKEND_ROOT, _OTHER_BACKEND_ROOT],
        )

        # Assert
        assert orphaned == [removed_name]

    def test_includes_legacy_format_database_without_live_worktree(self):
        # Arrange — worktree 削除済みのハッシュを持つ旧形式 DB
        removed_legacy_name = f"techradar_test_{worktree_hash(_REMOVED_BACKEND_ROOT)}"

        # Act
        orphaned = find_database_names_without_live_worktree(
            [removed_legacy_name],
            live_backend_roots=[_BACKEND_ROOT],
        )

        # Assert
        assert orphaned == [removed_legacy_name]

    @pytest.mark.parametrize(
        "database_name",
        [
            "techradar_test_deadbee",  # ハッシュ部分が1文字短い
            "techradar_test_deadbeefx",  # ハッシュ部分が1文字長い
            "techradar_test_DEADBEEF",  # 16進として不正（大文字）
            "some_other_database",  # 無関係な DB 名
        ],
    )
    def test_excludes_names_with_unparsable_hash_part(self, database_name: str):
        # Act
        orphaned = find_database_names_without_live_worktree(
            [database_name],
            live_backend_roots=[_BACKEND_ROOT],
        )

        # Assert — 解釈できない名前は消さない側に倒す
        assert orphaned == []

    def test_returns_all_valid_format_names_when_no_live_worktrees(self):
        """生存 worktree が1件も無い（＝一覧取得は成功したが空）場合、
        新旧どちらの形式の DB もすべて候補として返る。
        """
        # Arrange
        new_format_name = build_database_name(_BACKEND_ROOT, 333)
        legacy_format_name = f"techradar_test_{worktree_hash(_OTHER_BACKEND_ROOT)}"

        # Act
        orphaned = find_database_names_without_live_worktree(
            [new_format_name, legacy_format_name],
            live_backend_roots=[],
        )

        # Assert
        assert orphaned == [new_format_name, legacy_format_name]

    def test_mixed_live_and_removed_worktrees(self):
        """生存 worktree の DB と削除済み worktree の DB が混在していても
        削除済み worktree 分だけを候補にする。
        """
        # Arrange
        live_name = build_database_name(_BACKEND_ROOT, 1)
        removed_name = build_database_name(_REMOVED_BACKEND_ROOT, 2)
        live_legacy_name = f"techradar_test_{worktree_hash(_OTHER_BACKEND_ROOT)}"

        # Act
        orphaned = find_database_names_without_live_worktree(
            [live_name, removed_name, live_legacy_name],
            live_backend_roots=[_BACKEND_ROOT, _OTHER_BACKEND_ROOT],
        )

        # Assert
        assert orphaned == [removed_name]
