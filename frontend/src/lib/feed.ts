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

/**
 * フィード（`GET /api/feed`）の絞り込み条件（Issue #90）。
 *
 * `ArticleFilters`（`interest-articles.ts`）と同じ設計を踏襲する。トピック・
 * 技術タグは LLM が記事ごとに分類した自由文字列で、backend にも列挙された
 * 一覧は存在しないため、UI 側は自由入力（複数可）で受け付ける。
 */
export interface FeedFilters {
  /** 検索語。title/translated_title/summary_jaへの部分一致。 */
  q: string | null;
  /** 複数指定時は指定した全てを含む記事に絞る（AND、backend 側の仕様）。 */
  topics: string[];
  /** 複数指定時は指定した全てを含む記事に絞る（AND、backend 側の仕様）。 */
  technologies: string[];
  /** 公開日の下限（ISO8601、含む）。`published_at` が null の記事は `fetched_at` で代替される（backend 側の仕様）。 */
  publishedFrom: string | null;
  /** 公開日の上限（ISO8601、含む）。 */
  publishedTo: string | null;
  sourceDomain: string | null;
  /**
   * フィードの対象期間（日数）。`null` は backend の既定
   * （`config/scoring.yaml` の `freshness.max_age_days`）に任せることを表す。
   *
   * 公開日の範囲（`publishedFrom` / `publishedTo`）とは別の軸である。backend は
   * 候補そのものをこの期間で切ったうえで公開日の範囲を適用するため、対象期間より
   * 古い日付を範囲に指定しても 0 件にしかならない（Issue #90 自己レビュー）。
   */
  maxAgeDays: number | null;
}

/** 対象期間として指定できる日数の下限（`api/recommendations.py` と揃える）。 */
export const MIN_FEED_MAX_AGE_DAYS = 1;

/** 対象期間として指定できる日数の上限（`api/recommendations.py` と揃える）。 */
export const MAX_FEED_MAX_AGE_DAYS = 180;

export const EMPTY_FEED_FILTERS: FeedFilters = {
  q: null,
  topics: [],
  technologies: [],
  publishedFrom: null,
  publishedTo: null,
  sourceDomain: null,
  maxAgeDays: null,
};

/**
 * ISO8601 文字列として妥当なら（`Date` としてパース可能なら）そのまま返し、そうでなければ
 * `null` に落とす。共有リンク・ブラウザ履歴・手動編集で URL のクエリは容易に壊れうるため、
 * ここで検証しておくことで `publishedFrom` / `publishedTo` が不正な値のまま
 * バックエンドへ送られる（`GET /api/feed` の 422）ことも、`isoToJstDateInputValue`
 * のクラッシュを誘発することも防ぐ（`interest-articles.ts` の
 * `parseValidIsoDateOrNull` と同じ狙い）。
 */
function parseValidIsoDateOrNull(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  return Number.isNaN(new Date(value).getTime()) ? null : value;
}

/**
 * 対象期間の日数として妥当（整数かつ 1〜180）ならその値を、そうでなければ `null` を返す。
 *
 * `parseValidIsoDateOrNull` と同じ狙いで、壊れた URL クエリをそのまま
 * `GET /api/feed` へ送って 422 にしないための防御。範囲外は「指定なし」として
 * backend の既定へ落とす。
 *
 * URL からの復元（この下の `parseFeedFiltersFromSearchParams`）と、フォーム送信時の
 * 検証（`FeedFilterPanel`）の両方から使う。境界値の判定を1箇所に置くため export する。
 */
export function parseMaxAgeDaysOrNull(value: string | null): number | null {
  if (value === null) {
    return null;
  }
  const days = Number(value);
  if (!Number.isInteger(days) || days < MIN_FEED_MAX_AGE_DAYS || days > MAX_FEED_MAX_AGE_DAYS) {
    return null;
  }
  return days;
}

/**
 * URL のクエリパラメータからフィルター条件を復元する。キー名は backend のクエリ
 * パラメータ名（`q` / `source_domain` 等）とそのまま揃えており、URL 表示と
 * API リクエストの間で別々の変換テーブルを持たずに済むようにしている
 * （`buildSearchParamsFromFilters` と対になる）。
 */
export function parseFeedFiltersFromSearchParams(searchParams: URLSearchParams): FeedFilters {
  return {
    q: searchParams.get("q"),
    topics: searchParams.getAll("topics"),
    technologies: searchParams.getAll("technologies"),
    publishedFrom: parseValidIsoDateOrNull(searchParams.get("published_from")),
    publishedTo: parseValidIsoDateOrNull(searchParams.get("published_to")),
    sourceDomain: searchParams.get("source_domain"),
    maxAgeDays: parseMaxAgeDaysOrNull(searchParams.get("max_age_days")),
  };
}

/**
 * フィルター条件を `URLSearchParams` へ変換する。ブラウザ URL の更新にも
 * `GET /api/feed` のクエリ組み立てにも同じ関数を使う（キー名が一致しているため）。
 */
export function buildSearchParamsFromFilters(filters: FeedFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.q) {
    params.set("q", filters.q);
  }
  filters.topics.forEach((topic) => params.append("topics", topic));
  filters.technologies.forEach((technology) => params.append("technologies", technology));
  if (filters.publishedFrom) {
    params.set("published_from", filters.publishedFrom);
  }
  if (filters.publishedTo) {
    params.set("published_to", filters.publishedTo);
  }
  if (filters.sourceDomain) {
    params.set("source_domain", filters.sourceDomain);
  }
  if (filters.maxAgeDays !== null) {
    params.set("max_age_days", String(filters.maxAgeDays));
  }
  return params;
}

const JST_OFFSET = "+09:00";

/**
 * `<input type="date">` の値（`YYYY-MM-DD`）を、JST のその日 0:00 を表す UTC の
 * ISO8601 文字列へ変換する（期間下限用）。単一ユーザー・日本語UI前提のため、
 * `format-date.ts` と同じく JST 固定で扱う（タイムゾーン込みで送るぶんには
 * 実行環境に依存しない）。
 */
export function jstDateToPublishedFromIso(dateString: string): string {
  return new Date(`${dateString}T00:00:00.000${JST_OFFSET}`).toISOString();
}

/** `<input type="date">` の値を、JST のその日 23:59:59.999 を表す UTC の ISO8601 文字列へ変換する（期間上限用）。 */
export function jstDateToPublishedToIso(dateString: string): string {
  return new Date(`${dateString}T23:59:59.999${JST_OFFSET}`).toISOString();
}

/**
 * ISO8601 文字列（UTC）を JST の日付に直し、`<input type="date">` へ渡せる `YYYY-MM-DD` にする。
 *
 * `isoString` が不正で `Date` としてパースできない場合は空文字列を返す。呼び出し元
 * （`FeedFilterPanel` の `defaultValue`）はレンダー中にこれを呼ぶため、
 * `.toISOString()` の `RangeError` をここで防がないとページ全体がクラッシュする
 * （`parseFeedFiltersFromSearchParams` 側で不正値を弾いていても、防御的にここでも弾く）。
 */
export function isoToJstDateInputValue(isoString: string): string {
  const utcMillis = new Date(isoString).getTime();
  if (Number.isNaN(utcMillis)) {
    return "";
  }
  const jstMillis = utcMillis + 9 * 60 * 60 * 1000;
  return new Date(jstMillis).toISOString().slice(0, 10);
}

interface GetFeedOptions {
  /** 1始まりのページ番号。省略時は backend の既定値（1）。 */
  page?: number;
  limit?: number;
}

/**
 * フィードを取得する（`page` / `limit` の番号付きページング、Issue #90）。
 *
 * 絞り込み条件が変わると別の run（推薦の並び）を作り直す設計のため、cursor と
 * 異なり `page` は同じ条件の間だけ意味を持つ。呼び出し元（`useFeed`）は
 * フィルターが変わるたびにページ番号を1へ戻す。
 */
export function getFeed(filters: FeedFilters, { page, limit }: GetFeedOptions = {}): Promise<FeedResponse> {
  const query = buildSearchParamsFromFilters(filters);
  if (page !== undefined) {
    query.set("page", String(page));
  }
  if (limit !== undefined) {
    query.set("limit", String(limit));
  }

  const queryString = query.toString();
  return apiFetch<FeedResponse>(`/api/feed${queryString ? `?${queryString}` : ""}`);
}
