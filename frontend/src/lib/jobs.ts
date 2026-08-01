/**
 * ジョブ進捗確認 API クライアント（`backend/src/techradar/api/jobs.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出する。
 */

import { apiFetch } from "@/lib/api";
import type { components } from "@/lib/api-schema";

export type Job = components["schemas"]["JobResponse"];

export function getJob(jobId: string): Promise<Job> {
  return apiFetch<Job>(`/api/jobs/${encodeURIComponent(jobId)}`);
}
