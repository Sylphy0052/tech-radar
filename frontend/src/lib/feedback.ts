/**
 * 記事フィードバック API クライアント（`backend/src/techradar/api/feedback.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出する。
 */

import { apiFetch } from "@/lib/api";
import type { components } from "@/lib/api-schema";

export type FeedbackAction = components["schemas"]["FeedbackAction"];
export type BadReason = components["schemas"]["BadReason"];
export type ArticleFeedback = components["schemas"]["ArticleFeedbackResponse"];

type ArticleFeedbackCreate = components["schemas"]["ArticleFeedbackCreate"];

/**
 * Bad の理由（`PROJECT_SPEC.md` §7.2）の日本語ラベル。backend は分類値
 * （英語の識別子）をそのまま返す設計のため、UI 側で一箇所に集約して日本語化する。
 */
export const BAD_REASON_LABELS: Record<BadReason, string> = {
  not_interested: "テーマに興味がない",
  too_shallow: "内容が浅い",
  already_known: "既知の内容",
  promotional: "宣伝的",
  untrusted_source: "情報源を信頼できない",
  too_repetitive: "同じ内容を見すぎた",
};

interface SendFeedbackInput {
  action: FeedbackAction;
  /** Bad のときのみ意味を持つ任意項目。未指定なら送信しない。 */
  reason?: BadReason | null;
}

/** 記事へ Good / Bad / 保存を記録する。 */
export function sendFeedback(
  articleId: string,
  { action, reason }: SendFeedbackInput,
): Promise<ArticleFeedback> {
  // reason 未指定時にキー自体を省く。null を明示送信するのと区別する必要がないため、
  // 常に送るより意図が伝わる形にする。
  const payload: ArticleFeedbackCreate = reason === undefined ? { action } : { action, reason };

  return apiFetch<ArticleFeedback>(`/api/articles/${encodeURIComponent(articleId)}/feedback`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * 記事へのフィードバックを取り消す。backend は 204 No Content を返すため、
 * 戻り値は無い（`apiFetch` は 204 を undefined として返す）。
 */
export function deleteFeedback(articleId: string): Promise<void> {
  return apiFetch<void>(`/api/articles/${encodeURIComponent(articleId)}/feedback`, {
    method: "DELETE",
  });
}
