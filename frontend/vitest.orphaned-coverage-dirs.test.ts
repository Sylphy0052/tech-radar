/**
 * `vitest.orphaned-coverage-dirs.ts` のテスト（Issue #33）。
 *
 * 同じ worktree で `npm test`（vitest）を複数プロセス同時実行したときに、
 * カバレッジ出力先（`coverage/<pid>`）の孤児ディレクトリ判定を実ファイルシステム
 * を使わずに検証する。実際のディレクトリ削除・PID生存確認は
 * `vitest.global-setup.ts` 側の責務であり、ここでは扱わない。
 */
import { describe, expect, it } from "vitest";

import { findOrphanedCoverageDirectoryNames, parsePidFromDirectoryName } from "./vitest.orphaned-coverage-dirs";
import { TEST_TIMEOUT_MS } from "./src/test-utils/timeouts";

describe("parsePidFromDirectoryName", () => {
  it(
    "parses a purely numeric directory name as a PID",
    () => {
      // Act
      const pid = parsePidFromDirectoryName("12345");

      // Assert
      expect(pid).toBe(12345);
    },
    TEST_TIMEOUT_MS,
  );

  it.each([
    "lcov.info", // 過去の実行が残したレポートファイル
    ".tmp", // vitest自身が作る一時ディレクトリ
    "notapid", // 非数字
    "", // 空文字列
    "123abc", // 数字始まりだが非数字を含む
  ])(
    "returns null for a name that cannot be interpreted as a PID: %s",
    (name) => {
      // Act / Assert
      expect(parsePidFromDirectoryName(name)).toBeNull();
    },
    TEST_TIMEOUT_MS,
  );
});

describe("findOrphanedCoverageDirectoryNames", () => {
  it(
    "returns only directories whose PID is dead and not our own",
    () => {
      // Arrange — 死んだPID / 生存中PID / 自分自身 / 解釈不能 を混在させる
      const ownPid = 100;
      const deadPidName = "200";
      const alivePidName = "300";
      const ownName = String(ownPid);
      const unparsableName = "lcov.info";
      const existingNames = [deadPidName, alivePidName, ownName, unparsableName];
      const isPidAlive = (pid: number): boolean => pid === 300; // 300のみ生存している想定

      // Act
      const orphans = findOrphanedCoverageDirectoryNames(existingNames, { ownPid, isPidAlive });

      // Assert — 死んだPIDのディレクトリだけが孤児判定される
      expect(orphans).toEqual([deadPidName]);
    },
    TEST_TIMEOUT_MS,
  );

  it(
    "returns an empty array when no orphans exist",
    () => {
      // Arrange
      const existingNames = ["100"];

      // Act
      const orphans = findOrphanedCoverageDirectoryNames(existingNames, {
        ownPid: 100,
        isPidAlive: () => true,
      });

      // Assert
      expect(orphans).toEqual([]);
    },
    TEST_TIMEOUT_MS,
  );

  it(
    "excludes the own PID directory even if isPidAlive would report it as dead",
    () => {
      // Arrange — 自分自身のPIDは常に除外する（isPidAliveの実装ミスに対する安全弁）
      const ownPid = 100;
      const existingNames = [String(ownPid)];

      // Act
      const orphans = findOrphanedCoverageDirectoryNames(existingNames, {
        ownPid,
        isPidAlive: () => false,
      });

      // Assert
      expect(orphans).toEqual([]);
    },
    TEST_TIMEOUT_MS,
  );

  it(
    "excludes names that cannot be interpreted as a PID",
    () => {
      // Arrange
      const existingNames = ["lcov.info", ".tmp", "not-a-pid"];

      // Act
      const orphans = findOrphanedCoverageDirectoryNames(existingNames, {
        ownPid: 1,
        isPidAlive: () => false,
      });

      // Assert
      expect(orphans).toEqual([]);
    },
    TEST_TIMEOUT_MS,
  );
});
