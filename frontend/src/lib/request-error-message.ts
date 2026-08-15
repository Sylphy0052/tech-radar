/**
 * 通信失敗（ネットワークエラー・5xx 等）をユーザー向けメッセージへ変換する。
 *
 * `ApiError`（`api.ts`）が保持するステータスコードや、`fetch` 自体が投げる
 * ネットワークエラー（`TypeError` 等）を吸収し、詳細を画面に出さずに
 * 分かりやすい日本語メッセージへ写像する。
 */

import { ApiError, isRateLimitError } from "@/lib/api";

const SERVER_ERROR_MESSAGE = "サーバーでエラーが発生しました。しばらくしてから再度お試しください。";
const NETWORK_ERROR_MESSAGE = "通信に失敗しました。しばらくしてから再度お試しください。";
const RATE_LIMIT_MESSAGE = "リクエストが多すぎます。しばらくしてから再度お試しください。";

const SERVER_ERROR_STATUS_THRESHOLD = 500;

/**
 * レート制限（429）は通信障害ではなく意図的な拒否なので、待てば回復すると伝える。
 *
 * 待機中に残り秒数を数え直して再提示する仕組みは無い。表示した秒数はその時点の
 * 値のまま止まる。以前は `useFeed` が無限スクロール（`IntersectionObserver`）の
 * 自動再試行を止めるためにカウントダウンを持っており、この関数はそのために
 * export していたが、番号付きページングへ書き換えたとき（Issue #90）に一緒に
 * 消えた。ページャはユーザーのクリック起点でしか fetch しないので、自動で
 * 過剰にリクエストを送る経路そのものが無くなっている（Issue #101）。
 */
function getRateLimitMessage(retryAfterSeconds: number | null): string {
  // 0 秒（境界ちょうどで弾かれた場合）は「約0秒後」が不自然なので秒数を出さない。
  if (retryAfterSeconds === null || retryAfterSeconds <= 0) {
    return RATE_LIMIT_MESSAGE;
  }
  return `リクエストが多すぎます。約${retryAfterSeconds}秒後に再度お試しください。`;
}

export function getRequestErrorMessage(error: unknown): string {
  if (isRateLimitError(error)) {
    return getRateLimitMessage(error.retryAfterSeconds);
  }
  if (error instanceof ApiError && error.status >= SERVER_ERROR_STATUS_THRESHOLD) {
    return SERVER_ERROR_MESSAGE;
  }
  return NETWORK_ERROR_MESSAGE;
}
