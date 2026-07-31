/**
 * バックエンド API クライアント。
 *
 * すべてのリクエストはこのモジュールを経由させ、ベース URL とエラー処理を一箇所に集約する。
 */

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
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });

  if (!response.ok) {
    // エラーは握りつぶさず、ステータスと本文を保持して呼び出し側へ渡す。
    const body = await response.text();
    throw new ApiError(response.status, body || response.statusText);
  }

  return (await response.json()) as T;
}

export type Health = {
  status: string;
  version: string;
  brave_search_enabled: boolean;
};

export function getHealth(): Promise<Health> {
  return apiFetch<Health>("/api/health");
}
