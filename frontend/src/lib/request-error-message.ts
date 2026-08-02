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

/** レート制限（429）は通信障害ではなく意図的な拒否なので、待てば回復すると伝える。 */
function getRateLimitMessage(retryAfterSeconds: number | null): string {
  if (retryAfterSeconds === null) {
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
