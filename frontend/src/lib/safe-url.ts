/**
 * リンクとして描画してよい URL かどうかを判定する多層防御。
 *
 * `original_url` は backend 側（URL 登録時のスキーム検証・クロールが HTTP(S) でしか
 * 取得しないこと・canonical URL のホスト一致チェック）で既に守られており、危険な
 * スキームが入る経路は無い。とはいえ描画側にも独立したガードを置き、`<a href>` へ
 * そのまま渡す前に http/https 以外のスキーム（`javascript:` など）や解析できない
 * 文字列を弾く。
 */

const SAFE_URL_SCHEMES = ["http:", "https:"] as const;

export function isSafeHttpUrl(rawUrl: string): boolean {
  let parsed: URL;
  try {
    // 不正な文字列（スキーム無し・壊れた URL 等）は例外を投げるため必ず捕捉する。
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  return (SAFE_URL_SCHEMES as readonly string[]).includes(parsed.protocol);
}
