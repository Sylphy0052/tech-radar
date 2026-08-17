/**
 * 関心分析 API クライアント（`backend/src/techradar/api/interests.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出する（`interest-articles.ts` と同じ流儀）。
 */

import { apiFetch } from "@/lib/api";
import { CONTENT_TYPE_LABELS } from "@/lib/interest-articles";
import type { components } from "@/lib/api-schema";

export type InterestSummaryResponse = components["schemas"]["InterestSummaryResponse"];
export type InterestGenreItem = components["schemas"]["InterestGenreItem"];
export type InterestFeedbackRatio = components["schemas"]["InterestFeedbackRatio"];
export type InterestTechnologyItem = components["schemas"]["InterestTechnologyItem"];
export type InterestPrimarySourceRatio = components["schemas"]["InterestPrimarySourceRatio"];
export type InterestContentTypeItem = components["schemas"]["InterestContentTypeItem"];
export type InterestDifficultyItem = components["schemas"]["InterestDifficultyItem"];
export type SuppressedTopicItem = components["schemas"]["SuppressedTopicItem"];
export type InterestClusterItem = components["schemas"]["InterestClusterItem"];
export type InterestClusterListResponse = components["schemas"]["InterestClusterListResponse"];
export type InterestTimelineResponse = components["schemas"]["InterestTimelineResponse"];
export type InterestTimelineBucket = components["schemas"]["InterestTimelineBucket"];
export type InterestTimelineTopicStats = components["schemas"]["InterestTimelineTopicStats"];
export type InterestOriginCounts = components["schemas"]["InterestOriginCounts"];

/** `content_type` は `interest-articles.ts` の定義を再利用する（重複定義しない）。 */
export { CONTENT_TYPE_LABELS };

/** `difficulty`（`analysis/prompt.py` の分類）の日本語ラベル。未知の値はそのまま表示する。 */
export const DIFFICULTY_LABELS: Record<string, string> = {
  beginner: "初級",
  intermediate: "中級",
  advanced: "上級",
};

const UNCLASSIFIED_LABEL = "未分類";

/**
 * 内容分布系（技術・公式情報比率・記事の性質・難易度）に共通する注記。
 * バックエンド（`api/interests.py` の `get_interest_summary`）がこれらの集計を
 * `af.action IN ('good', 'save')` に絞って返すため、母集団の誤読を防ぐために
 * 画面側でも明示する。4箇所で同じ文言を重複させないよう1箇所にまとめる。
 */
export const GOOD_OR_SAVED_ONLY_NOTE = "Good・保存した記事のみを対象にしています。";

/**
 * `origin_counts`（Issue #92）に付ける注記。
 *
 * 他の可視化は `article_feedback` の good/save に絞った集計（上記
 * `GOOD_OR_SAVED_ONLY_NOTE`）だが、`origin_counts` は
 * `interest/service.py` の関心プロファイル構築対象（`user_articles` の
 * 手動登録・Good・保存・全文閲覧・クリックの5経路 + `article_feedback` の
 * good のみの取りこぼし補完）を数えた別の母集団のため、混同しないよう
 * 専用の注記にする。
 */
export const PROFILE_POPULATION_NOTE =
  "関心プロファイルの構築対象（手動登録・Good・保存・全文閲覧・クリック）を集計しています。";

/**
 * `domain` / `content_type` / `difficulty` の表示ラベルを1箇所で決める。
 *
 * `null`（未分類）は常に「未分類」に統一する。`labels` を渡した場合はそこから
 * 日本語ラベルを引き、無ければ値をそのまま表示する（未知の値のフォールバック）。
 * `domain` のように対応表を持たない項目は `labels` を省略し、値をそのまま返す。
 */
export function formatNullableLabel(value: string | null, labels?: Record<string, string>): string {
  if (value === null) {
    return UNCLASSIFIED_LABEL;
  }
  return labels ? (labels[value] ?? value) : value;
}

const WEEK_LABEL_FORMAT_OPTIONS: Intl.DateTimeFormatOptions = {
  month: "numeric",
  day: "numeric",
  // 単一ユーザー・日本語UI前提のため、`format-date.ts` と同じく JST 固定で表示する。
  timeZone: "Asia/Tokyo",
};

/** タイムラインのバケット開始日時（週初め、UTC）を `8/1週` のような短い表示へ整形する。 */
export function formatWeekLabel(isoString: string): string {
  const date = new Date(isoString);
  return `${date.toLocaleDateString("ja-JP", WEEK_LABEL_FORMAT_OPTIONS)}週`;
}

/** 関心分析サマリーを取得する（`GET /api/interests/summary`、Issue #16）。 */
export function getInterestSummary(): Promise<InterestSummaryResponse> {
  return apiFetch<InterestSummaryResponse>("/api/interests/summary");
}

/** 関心クラスタ一覧を取得する（`GET /api/interests/clusters`）。 */
export function listInterestClusters(): Promise<InterestClusterListResponse> {
  return apiFetch<InterestClusterListResponse>("/api/interests/clusters");
}

/**
 * 関心の推移を週単位で取得する（`GET /api/interests/timeline`）。
 *
 * `weeks` を省略した場合はバックエンド側の既定値（`config/scoring.yaml` の
 * `interest_timeline.default_weeks`）が使われる。
 */
export function getInterestTimeline(weeks?: number): Promise<InterestTimelineResponse> {
  const query = weeks !== undefined ? `?weeks=${weeks}` : "";
  return apiFetch<InterestTimelineResponse>(`/api/interests/timeline${query}`);
}
