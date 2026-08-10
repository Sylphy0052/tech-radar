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
export type BulkArticleImportResult = components["schemas"]["BulkArticleImportResponse"];

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

/**
 * URL リストファイル（.md/.txt、UTF-8）をアップロードし、一括登録する（Issue #39）。
 *
 * backend は `multipart/form-data` のフィールド名 `file` を要求する。JSON を送る
 * 他の関数と異なり `FormData` を渡すことで、`apiFetch` 側が自動でこれを検知し
 * Content-Type の既定値（application/json）を付けないようにする。
 */
export function bulkImportArticles(file: File): Promise<BulkArticleImportResult> {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch<BulkArticleImportResult>("/api/articles/bulk", {
    method: "POST",
    body: formData,
  });
}
