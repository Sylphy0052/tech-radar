/**
 * URL 登録フォームの送信前バリデーション。
 *
 * backend（`articles.py` の `_validate_scheme`）と同じ方針（http/https のみ許可）を
 * クライアント側でも先にチェックし、不正な URL でリクエストを送らないようにする。
 */

const ALLOWED_URL_SCHEMES = ["http:", "https:"] as const;

// スキームの有無だけを見る軽量チェック。「http://」のような壊れた URL と
// 「example.com/foo」のようなスキームが無い URL を区別するために使う
// （どちらも `new URL()` は例外を投げるが、ユーザーに返すべきメッセージが違う）。
const SCHEME_PREFIX_PATTERN = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

const EMPTY_URL_MESSAGE = "URLを入力してください";
const INVALID_SCHEME_MESSAGE = "httpまたはhttpsで始まるURLのみ登録できます";
const MALFORMED_URL_MESSAGE = "有効なURLの形式で入力してください";

function isAllowedScheme(scheme: string): boolean {
  return (ALLOWED_URL_SCHEMES as readonly string[]).includes(scheme);
}

/**
 * 入力された URL を検証し、問題があればユーザー向けの日本語エラーメッセージを返す。
 * 問題がなければ null を返す。
 */
export function validateArticleUrl(rawUrl: string): string | null {
  const trimmed = rawUrl.trim();
  if (trimmed === "") {
    return EMPTY_URL_MESSAGE;
  }

  if (!SCHEME_PREFIX_PATTERN.test(trimmed)) {
    // スキーム自体が無い（例: "example.com/articles/1"）。
    return INVALID_SCHEME_MESSAGE;
  }

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return MALFORMED_URL_MESSAGE;
  }

  if (!isAllowedScheme(parsed.protocol)) {
    return INVALID_SCHEME_MESSAGE;
  }

  return null;
}
