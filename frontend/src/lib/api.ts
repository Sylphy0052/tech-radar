/**
 * バックエンド API クライアント。
 *
 * すべてのリクエストはこのモジュールを経由させ、ベース URL とエラー処理を一箇所に集約する。
 *
 * レスポンス型は手書きせず、`api-schema.d.ts`（`npm run gen:api-types` が
 * `backend/openapi.json` から生成）から導出する。DB / API / UI 間の型不整合を
 * 防ぐため（`PROJECT_SPEC.md` §24）、生成型が最新かは `scripts/ai-harness/check.sh` で検証する。
 */

import type { components } from "@/lib/api-schema";

const DEFAULT_API_BASE_URL = "http://localhost:8000";

export function getApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return configured ? configured.replace(/\/+$/, "") : DEFAULT_API_BASE_URL;
}

/** API がエラーレスポンスを返したことを表す。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** `Retry-After` の待機秒数。ヘッダが無い / 秒数として読めない場合は null。 */
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const TOO_MANY_REQUESTS_STATUS = 429;

/** レート制限（`backend/src/techradar/api/rate_limit.py`）による拒否かどうか。 */
export function isRateLimitError(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === TOO_MANY_REQUESTS_STATUS;
}

/**
 * `Retry-After` ヘッダを待機秒数として解釈する。
 *
 * 規格上は HTTP-date 形式も許容されるが、backend（`api/rate_limit.py`）は
 * 整数秒しか返さない。delta-seconds として読めないものは、誤った待ち時間を
 * 表示するより「待ち時間不明」に倒すため null を返す。
 */
function parseRetryAfterSeconds(header: string | null): number | null {
  if (header === null || header.trim() === "") {
    return null;
  }
  const seconds = Number(header);
  if (!Number.isFinite(seconds) || seconds < 0) {
    return null;
  }
  return Math.ceil(seconds);
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });

  if (!response.ok) {
    // エラーは握りつぶさず、ステータスと本文を保持して呼び出し側へ渡す。
    const body = await response.text();
    throw new ApiError(
      response.status,
      body || response.statusText,
      parseRetryAfterSeconds(response.headers.get("Retry-After")),
    );
  }

  // 204 No Content はボディを持たないため json() を呼ぶとパースエラーになる。
  // 呼び出し側は T = void を指定する想定。
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export type Health = components["schemas"]["HealthResponse"];

export function getHealth(): Promise<Health> {
  return apiFetch<Health>("/api/health");
}
