/**
 * vitestのカバレッジ出力先をプロセス単位に分離するための純粋ロジック（Issue #33）。
 *
 * 同じ worktree で `npm test`（`vitest run --coverage`）を複数プロセス同時実行すると、
 * `coverage.reportsDirectory` の既定値（`coverage`）を取り合い、片方が
 * 「Something removed the coverage directory ... Vitest created earlier」で落ちる
 * （backend側でDBを PID ごとに分離した Issue #23/#33 と同型の問題）。
 * `reportsDirectory` を `coverage/<pid>` へ分離することで衝突を避け、異常終了で
 * 残った孤児ディレクトリは次回の実行開始時（`vitest.global-setup.ts`）に掃除する。
 *
 * このモジュールは「孤児ディレクトリの判定」という副作用の無いロジックだけを置く。
 * 実際のディレクトリ削除・PIDの生存確認は `vitest.global-setup.ts` 側が担う。
 * このモジュール自体のテストは `vitest.orphaned-coverage-dirs.test.ts` に置く。
 *
 * backendの `backend/tests/db_process_isolation.py` と同じ設計（判定ロジックを
 * 純粋関数として切り出し、生存確認をコールバックとして注入可能にする）に揃えている。
 * ただしworktreeごとに `coverage/` ディレクトリ自体が分かれる（別worktreeは別の
 * チェックアウトパス）ため、backendの `worktree_hash` に相当する分離は不要。
 */

/** `coverage/` 直下のディレクトリ名がPIDとして解釈できる形式かどうかの判定に使う。 */
const PID_DIRECTORY_NAME_PATTERN = /^[0-9]+$/;

/**
 * `coverage/` 直下のディレクトリ名からPIDを取り出す。
 *
 * 数字のみで構成された名前だけをPIDとして扱う。`lcov.info` や `.tmp` のような
 * 過去の実行が残したファイル・ディレクトリ、その他このモジュールが生成した
 * ものではない名前は全て `null` を返す（後続の孤児判定で安全側＝消さない側に
 * 倒すため）。
 */
export function parsePidFromDirectoryName(name: string): number | null {
  if (!PID_DIRECTORY_NAME_PATTERN.test(name)) {
    return null;
  }
  return Number(name);
}

/** {@link findOrphanedCoverageDirectoryNames} が孤児判定に使う情報。 */
export interface FindOrphanedCoverageDirectoryNamesOptions {
  /** 呼び出し元プロセス自身のPID。このPIDのディレクトリは孤児判定の対象外にする。 */
  ownPid: number;
  /** 指定したPIDのプロセスが生存しているかを返す関数。テストから偽の生存判定を注入できるようにコールバックにしている。 */
  isPidAlive: (pid: number) => boolean;
}

/**
 * `existingNames`（`coverage/` 直下のエントリ名一覧）のうち、孤児のカバレッジ
 * 出力ディレクトリの名前を返す。
 *
 * 孤児 = 異常終了した過去の vitest プロセスが後始末できずに残したディレクトリ。
 * 以下のいずれかに該当する名前は孤児と判定しない（安全側＝消さない側に倒す）:
 *
 * - 名前がPIDとして解釈できない（`parsePidFromDirectoryName` が `null` を返す）
 * - 自分自身（`ownPid`）のディレクトリ
 * - `isPidAlive(pid)` が真、つまりそのプロセスがまだ生きている
 *
 * 実際のディレクトリ削除は呼び出し側（`vitest.global-setup.ts`）が行う。
 */
export function findOrphanedCoverageDirectoryNames(
  existingNames: readonly string[],
  { ownPid, isPidAlive }: FindOrphanedCoverageDirectoryNamesOptions,
): string[] {
  const orphans: string[] = [];
  for (const name of existingNames) {
    const pid = parsePidFromDirectoryName(name);
    if (pid === null) {
      continue;
    }
    if (pid === ownPid) {
      continue;
    }
    if (isPidAlive(pid)) {
      continue;
    }
    orphans.push(name);
  }
  return orphans;
}
