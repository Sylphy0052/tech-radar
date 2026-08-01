/**
 * Discover フィード取得 API クライアント（`backend/src/techradar/api/recommendations.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出する。
 */

import { apiFetch } from "@/lib/api";
import type { components } from "@/lib/api-schema";

export type FeedResponse = components["schemas"]["FeedResponse"];

/** フィード 1 件分の記事。UI 側からはこの型を通して参照する。 */
export type FeedItem = components["schemas"]["RecommendationItem"];

interface GetFeedParams {
  /** 前回レスポンスの next_cursor をそのまま渡す。省略時は先頭ページを返す。 */
  cursor?: string | null;
  limit?: number;
}

/**
 * フィードを取得する。`cursor` は同じ run を rank 順に辿るためのものなので、
 * 途中のページからも取り直せるよう毎回そのまま backend へ転送する。
 */
export function getFeed({ cursor, limit }: GetFeedParams = {}): Promise<FeedResponse> {
  const query = new URLSearchParams();
  if (cursor) {
    query.set("cursor", cursor);
  }
  if (limit !== undefined) {
    query.set("limit", String(limit));
  }

  const queryString = query.toString();
  return apiFetch<FeedResponse>(`/api/feed${queryString ? `?${queryString}` : ""}`);
}
