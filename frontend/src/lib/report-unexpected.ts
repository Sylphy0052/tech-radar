/**
 * 「到達したらバグ」の分岐から、開発時にだけ痕跡を残すための警告。
 *
 * 対象が見つからない等の防御的な early return は、本番では黙って戻るのが正しい
 * （利用者に対処しようのない内部エラーを見せない）。一方で黙って戻るだけだと、
 * 実際に到達したときに何も手掛かりが残らない。Issue #45 で問題にした
 * 「クリックが握り潰されて、押しても何も起きないように見える」がまさにその形で、
 * 原因（`useFeed` が古い `items` を読んでいた）に辿り着くまで時間を要した。
 *
 * `NODE_ENV` は Next.js のビルド時に置換されるため、本番バンドルではこの関数の
 * 中身は到達不能なコードになる。vitest では `"test"` のため、テストからは警告を
 * 観測できる。
 */
const LOG_PREFIX = "[techradar]";

export function reportUnexpectedState(message: string): void {
  if (process.env.NODE_ENV === "production") {
    return;
  }
  // 本番では上で戻るため、ここは開発ビルドでしか実行されない。
  // （`no-console` はこのリポジトリの eslint 設定では有効になっていないため、
  // 抑制コメントは付けない。付けると「不要な抑制」として警告になる）
  console.warn(`${LOG_PREFIX} ${message}`);
}
