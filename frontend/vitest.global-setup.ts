/**
 * vitestのグローバルセットアップ（Issue #33）。
 *
 * `vitest.config.mts` で `coverage.reportsDirectory` を `coverage/<pid>` に
 * 分離した分、異常終了（Ctrl-C・クラッシュ等）した過去のプロセスが後始末できずに
 * 残した孤児ディレクトリを、新しいセッション開始時にここで掃除する。孤児かどうかの
 * 判定そのものは副作用の無い `vitest.orphaned-coverage-dirs.ts` に任せ、ここでは
 * 実際のディレクトリ一覧取得・PID生存確認・削除だけを行う。
 *
 * globalSetupファイルはテスト実行プロセス（メインプロセス）内で、テストファイルの
 * 実行前に一度だけ呼ばれる（vitest v4、`TestProject#_initializeGlobalSetup`）。
 * `vitest.config.mts` の `process.pid` と同じプロセスで動くため、自分のPIDに
 * 対応する `coverage/<pid>` ディレクトリを孤児判定から確実に除外できる。
 */
import { existsSync } from "node:fs";
import { readdir, rm } from "node:fs/promises";
import { join } from "node:path";

import { findOrphanedCoverageDirectoryNames } from "./vitest.orphaned-coverage-dirs";

const COVERAGE_ROOT = join(import.meta.dirname, "coverage");

/**
 * 指定PIDのプロセスが生存しているかを判定する。
 *
 * シグナル番号 0 はプロセスを実際には終了させず、存在確認だけを行う
 * （`kill(2)` の慣用的な使い方。backendの `_pid_is_alive` と同じ考え方）。
 * `ESRCH`（存在しない）以外のエラー（例: 権限不足で別ユーザーのプロセスを
 * 確認できない `EPERM`）は、孤児ディレクトリの誤削除を避けるため
 * 「生存している」側に倒す。
 */
function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return code !== "ESRCH";
  }
}

/**
 * `coverage/` 配下の孤児ディレクトリを掃除する。
 *
 * `coverage/` ディレクトリがまだ存在しない（初回実行）場合は何もしない。
 * 個々のディレクトリ削除に失敗しても（他プロセスが使用中等）、テスト実行
 * 自体は止めずに警告だけ出す。掃除はベストエフォートであり、次回以降の
 * セッションでも再試行されるため。
 */
async function cleanupOrphanedCoverageDirectories(): Promise<void> {
  if (!existsSync(COVERAGE_ROOT)) {
    return;
  }

  const entries = await readdir(COVERAGE_ROOT);
  const orphanNames = findOrphanedCoverageDirectoryNames(entries, {
    ownPid: process.pid,
    isPidAlive,
  });

  await Promise.all(
    orphanNames.map(async (name) => {
      try {
        await rm(join(COVERAGE_ROOT, name), { recursive: true, force: true });
      } catch (error) {
        console.warn(`[vitest.global-setup] 孤児カバレッジディレクトリの削除に失敗しました: ${name}`, error);
      }
    }),
  );
}

export default async function setup(): Promise<void> {
  await cleanupOrphanedCoverageDirectories();
}
