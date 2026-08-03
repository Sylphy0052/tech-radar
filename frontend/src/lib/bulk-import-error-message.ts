/**
 * 一括インポート（`POST /api/articles/bulk`）専用のエラーメッセージ変換。
 *
 * 413（ファイルサイズ/URL件数の上限超過）と422（拡張子不正・UTF-8デコード不能）は
 * このエンドポイント固有の意味を持つため専用メッセージへ写像する。それ以外
 * （5xx・ネットワークエラー・429 等）は既存の `getRequestErrorMessage`
 * （`request-error-message.ts`）にそのまま委譲し、重複ロジックを作らない。
 */

import { ApiError } from "@/lib/api";
import { getRequestErrorMessage } from "@/lib/request-error-message";

const PAYLOAD_TOO_LARGE_STATUS = 413;
const UNSUPPORTED_FORMAT_STATUS = 422;

const PAYLOAD_TOO_LARGE_MESSAGE =
  "ファイルが大きすぎるか、URLの件数が多すぎます（1MB以内、500件以内にしてください）";
const UNSUPPORTED_FORMAT_MESSAGE =
  "対応していないファイル形式です（.md / .txt のUTF-8テキストのみ対応しています）";

export function getBulkImportErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === PAYLOAD_TOO_LARGE_STATUS) {
    return PAYLOAD_TOO_LARGE_MESSAGE;
  }
  if (error instanceof ApiError && error.status === UNSUPPORTED_FORMAT_STATUS) {
    return UNSUPPORTED_FORMAT_MESSAGE;
  }
  return getRequestErrorMessage(error);
}
