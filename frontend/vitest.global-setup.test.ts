/**
 * `vitest.global-setup.ts` の統合テスト（Issue #33 self review 対応）。
 *
 * `cleanupOrphanedCoverageDirectories` が実際に呼ぶ readdir/rm/process.kill を
 * 実ファイルシステム上の一時ディレクトリに対して動かし、「生存していないPID名の
 * ディレクトリだけが消える」「PIDとして解釈できない名前は残る」を検証する。
 * 孤児かどうかの判定そのもの（純粋ロジック）は `vitest.orphaned-coverage-dirs.test.ts`
 * で既に検証済みのため、ここでは実ファイルシステムへの副作用のみを見る。
 *
 * 掃除対象のルートは `cleanupOrphanedCoverageDirectories(coverageRoot)` の引数で
 * 差し替えられるようリファクタ済み。実行中の自分自身の `coverage/` を巻き込まない
 * よう、テストは必ず専用の一時ディレクトリを渡す（既定の `coverage/` を対象にした
 * 挙動そのものはテストしない）。
 */
import { mkdir, mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { cleanupOrphanedCoverageDirectories } from "./vitest.global-setup";
import { TEST_TIMEOUT_MS } from "./src/test-utils/timeouts";

// OS上に実在しえない極端に大きいPID（32bit符号付きpid_tの上限付近）。
// 実プロセスをフォーク/待機させずに「確実に死んでいるPID」を得るために使う。
const DEAD_PID = 2147483647;

let tempRoot: string | undefined;

afterEach(async () => {
  if (tempRoot !== undefined) {
    await rm(tempRoot, { recursive: true, force: true });
    tempRoot = undefined;
  }
}, TEST_TIMEOUT_MS);

describe("cleanupOrphanedCoverageDirectories", () => {
  it(
    "removes only the directory whose PID is dead, and keeps names that cannot be parsed as a PID",
    async () => {
      // Arrange — 死んだPID / 自分自身のPID（生存中）/ 解釈不能な名前を混在させる
      tempRoot = await mkdtemp(join(tmpdir(), "vitest-global-setup-test-"));
      const deadPidDir = join(tempRoot, String(DEAD_PID));
      const ownPidDir = join(tempRoot, String(process.pid));
      const unparsableDir = join(tempRoot, "lcov.info");
      await mkdir(deadPidDir);
      await mkdir(ownPidDir);
      await mkdir(unparsableDir);

      // Act
      await cleanupOrphanedCoverageDirectories(tempRoot);

      // Assert — 死んだPIDのディレクトリだけが消え、他は残る
      const remaining = await readdir(tempRoot);
      expect(remaining.sort()).toEqual([String(process.pid), "lcov.info"].sort());
    },
    TEST_TIMEOUT_MS,
  );

  it(
    "does nothing when the root directory does not exist",
    async () => {
      // Arrange — mkdirせず、存在しないパスのみ用意する
      tempRoot = join(tmpdir(), `vitest-global-setup-test-nonexistent-${process.pid}`);

      // Act / Assert — 例外を投げずに正常終了する
      await expect(cleanupOrphanedCoverageDirectories(tempRoot)).resolves.toBeUndefined();
    },
    TEST_TIMEOUT_MS,
  );
});
