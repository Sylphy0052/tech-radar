/**
 * 記事 URL 登録・登録状態確認 API クライアント（`backend/src/techradar/api/articles.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出し、手書きの
 * 重複定義を作らない。
 */

import { apiFetch } from "@/lib/api";
import type { components } from "@/lib/api-schema";

export type ArticleRegistration = components["schemas"]["ArticleRegistrationResponse"];
type ArticleRegistrationCreate = components["schemas"]["ArticleRegistrationCreate"];

/**
 * URL を登録する。同じ URL を登録済みの場合、backend は新規作成せず
 * 既存登録を 200 で返す（`ApiError` にはならない）。
 */
export function registerArticle(url: string): Promise<ArticleRegistration> {
  const payload: ArticleRegistrationCreate = { url };
  return apiFetch<ArticleRegistration>("/api/articles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getArticleRegistration(registrationId: string): Promise<ArticleRegistration> {
  return apiFetch<ArticleRegistration>(
    `/api/articles/registrations/${encodeURIComponent(registrationId)}`,
  );
}
