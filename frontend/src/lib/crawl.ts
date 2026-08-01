/**
 * 巡回起動 API クライアント（`backend/src/techradar/api/crawl.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出する。
 */

import { apiFetch } from "@/lib/api";
import type { components } from "@/lib/api-schema";

export type CrawlRun = components["schemas"]["CrawlRunResponse"];

/**
 * 巡回ジョブを起動する。進行中の巡回が既にある場合、backend は新規作成せず
 * 既存ジョブを 200 で返す（`ApiError` にはならない）。source_domain は
 * MVP の UI からは指定しないため、常に全件対象で起動する。
 */
export function startCrawlRun(): Promise<CrawlRun> {
  return apiFetch<CrawlRun>("/api/crawl/runs", {
    method: "POST",
    body: JSON.stringify({}),
  });
}
