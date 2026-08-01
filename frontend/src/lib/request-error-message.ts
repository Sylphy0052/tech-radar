/**
 * 通信失敗（ネットワークエラー・5xx 等）をユーザー向けメッセージへ変換する。
 *
 * `ApiError`（`api.ts`）が保持するステータスコードや、`fetch` 自体が投げる
 * ネットワークエラー（`TypeError` 等）を吸収し、詳細を画面に出さずに
 * 分かりやすい日本語メッセージへ写像する。
 */

import { ApiError } from "@/lib/api";

const SERVER_ERROR_MESSAGE = "サーバーでエラーが発生しました。しばらくしてから再度お試しください。";
const NETWORK_ERROR_MESSAGE = "通信に失敗しました。しばらくしてから再度お試しください。";

const SERVER_ERROR_STATUS_THRESHOLD = 500;

export function getRequestErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status >= SERVER_ERROR_STATUS_THRESHOLD) {
    return SERVER_ERROR_MESSAGE;
  }
  return NETWORK_ERROR_MESSAGE;
}
