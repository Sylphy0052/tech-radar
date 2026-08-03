/**
 * `waitFor` の持ち時間（ミリ秒）。ファイル単位で `configure` へ渡して使う。
 *
 * 既定の1秒は、テストファイル並列実行でCPUが奪われているときに
 * 「fetchのモックが解決→state更新→再レンダー」までを賄えないことがある
 * （Issue #29, #30, #35の失敗はいずれもこの形）。個々の `waitFor` へ都度
 * 指定すると、付け忘れた待機だけが脆いまま残るため、ファイル単位で引き上げる。
 *
 * この値を超えて待つ必要が出たときは、待ち時間を伸ばす前に「待っている条件が
 * そもそも成立しうるのか」を疑うこと。Issue #37では、10秒へ伸ばしてもなお
 * 落ちる待機があり、原因は遅さではなくクリックが黙って捨てられる実装側の
 * 競合だった（`useFeed` / `useInterestArticles` の items の読み取り）。
 */
export const WAIT_TIMEOUT_MS = 5_000;

/**
 * 上の待機を使うテストへ与える持ち時間（ミリ秒）。
 *
 * vitestの既定（5000ms）のままだと `WAIT_TIMEOUT_MS` と並んでしまい、待ち切る
 * 前にテスト側が先にタイムアウトする（Issue #35）。待機が本当に失敗したときに、
 * タイムアウトではなくassertの失敗として原因が読める形にするため引き上げる。
 *
 * `waitFor` を使わない完全同期のテストにも同じ値を配る。rechartsの同期レンダー
 * そのものが5秒を超えることを実測した（Issue #37）。`waitFor` を一切使わない
 * テスト（`InterestTimelineChart.test.tsx` の「renders the title and a legend
 * entry for each series」）でさえ、負荷の高い条件下では `Test timed out in
 * 5000ms` で落ちた。つまり原因は待機条件が成立しないことではなく、recharts を
 * 描画する処理そのものが vitest の既定 testTimeout 5000ms に収まらないことに
 * あり、待機の有無に関わらずファイル内の全テストへ一律に配る必要がある。
 *
 * グローバル設定（`vitest.config.mts`）は変えない。他のテストが実際にハング
 * したときの検出まで一律に遅くなるため（Issue #35の自己レビューで一度
 * 差し戻された判断）。
 */
export const TEST_TIMEOUT_MS = 20_000;
