/**
 * 関心記事一覧 API クライアント（`backend/src/techradar/api/articles.py`）。
 *
 * 型は `api-schema.d.ts`（openapi-typescript 生成）から導出する。
 */

import { apiFetch } from "@/lib/api";
import type { components } from "@/lib/api-schema";

export type InterestArticleItem = components["schemas"]["InterestArticleItem"];
export type InterestArticleListResponse = components["schemas"]["InterestArticleListResponse"];
export type ArticleOrigin = components["schemas"]["ArticleOrigin"];

/**
 * 関心記事一覧（`GET /api/articles`）が対象にする登録経路（`PROJECT_SPEC.md` §6.3）。
 * `ArticleOrigin` にはこの一覧に出ない経路（`read_full` / `clicked`）も含まれるため、
 * フィルター UI の選択肢はこの3つだけに絞る。
 */
export const INTEREST_ARTICLE_ORIGINS = [
  "manual",
  "good",
  "saved",
] as const satisfies readonly ArticleOrigin[];

export const ORIGIN_LABELS: Record<(typeof INTEREST_ARTICLE_ORIGINS)[number], string> = {
  manual: "手動登録",
  good: "Good",
  saved: "保存",
};

/** `content_type`（`analysis/prompt.py` の分類）の日本語ラベル。未知の値はそのまま表示する。 */
export const CONTENT_TYPE_LABELS: Record<string, string> = {
  concept: "概念解説",
  implementation: "実装・手順",
  research: "研究・論文",
  news: "発表・リリース",
};

/**
 * 登録方法の表示ラベルを返す。`InterestArticleItem.origin` の型は `ArticleOrigin`
 * （5値）だが、`GET /api/articles` は実際には常に3経路（`INTEREST_ARTICLE_ORIGINS`）
 * だけを返す（backend 側で絞り込み済み）。型としてはそれ以外の値も排除できないため、
 * `ORIGIN_LABELS` に無い値はそのまま表示するフォールバックを用意する。
 */
export function originLabel(origin: ArticleOrigin): string {
  return (ORIGIN_LABELS as Partial<Record<ArticleOrigin, string>>)[origin] ?? origin;
}

export interface ArticleFilters {
  origin: ArticleOrigin[];
  /** 検索語。title/translated_title/summary_jaへの部分一致（Issue #91）。 */
  q: string | null;
  /** 複数指定時は指定した全てを含む記事に絞る（AND、backend 側の仕様）。 */
  topics: string[];
  /** 複数指定時は指定した全てを含む記事に絞る（AND、backend 側の仕様）。 */
  technologies: string[];
  domain: string | null;
  category: string | null;
  sourceDomain: string | null;
  language: string | null;
  /** 期間下限（ISO8601、含む）。 */
  registeredFrom: string | null;
  /** 期間上限（ISO8601、含む）。 */
  registeredTo: string | null;
  isPrimarySource: boolean | null;
}

export const EMPTY_ARTICLE_FILTERS: ArticleFilters = {
  origin: [],
  q: null,
  topics: [],
  technologies: [],
  domain: null,
  category: null,
  sourceDomain: null,
  language: null,
  registeredFrom: null,
  registeredTo: null,
  isPrimarySource: null,
};

function isInterestArticleOrigin(value: string): value is ArticleOrigin {
  return (INTEREST_ARTICLE_ORIGINS as readonly string[]).includes(value);
}

/**
 * ISO8601 文字列として妥当なら（`Date` としてパース可能なら）そのまま返し、そうでなければ
 * `null` に落とす。共有リンク・ブラウザ履歴・手動編集で URL のクエリは容易に壊れうるため、
 * ここで検証しておくことで `registeredFrom` / `registeredTo` が不正な値のまま
 * バックエンドへ送られる（`GET /api/articles` の 422）ことも、`isoToJstDateInputValue`
 * のクラッシュを誘発することも防ぐ。
 */
function parseValidIsoDateOrNull(value: string | null): string | null {
  if (value === null) {
    return null;
  }
  return Number.isNaN(new Date(value).getTime()) ? null : value;
}

/**
 * URL のクエリパラメータからフィルター条件を復元する。キー名は backend のクエリ
 * パラメータ名（`origin` / `source_domain` 等）とそのまま揃えており、URL 表示と
 * API リクエストの間で別々の変換テーブルを持たずに済むようにしている
 * （`buildSearchParamsFromFilters` と対になる）。
 */
export function parseArticleFiltersFromSearchParams(searchParams: URLSearchParams): ArticleFilters {
  const isPrimarySourceRaw = searchParams.get("is_primary_source");
  return {
    origin: searchParams.getAll("origin").filter(isInterestArticleOrigin),
    q: searchParams.get("q"),
    topics: searchParams.getAll("topics"),
    technologies: searchParams.getAll("technologies"),
    domain: searchParams.get("domain"),
    category: searchParams.get("category"),
    sourceDomain: searchParams.get("source_domain"),
    language: searchParams.get("language"),
    registeredFrom: parseValidIsoDateOrNull(searchParams.get("registered_from")),
    registeredTo: parseValidIsoDateOrNull(searchParams.get("registered_to")),
    isPrimarySource: isPrimarySourceRaw === null ? null : isPrimarySourceRaw === "true",
  };
}

/**
 * フィルター条件を `URLSearchParams` へ変換する。ブラウザ URL の更新にも
 * `GET /api/articles` のクエリ組み立てにも同じ関数を使う（キー名が一致しているため）。
 */
export function buildSearchParamsFromFilters(filters: ArticleFilters): URLSearchParams {
  const params = new URLSearchParams();
  filters.origin.forEach((origin) => params.append("origin", origin));
  if (filters.q) {
    params.set("q", filters.q);
  }
  filters.topics.forEach((topic) => params.append("topics", topic));
  filters.technologies.forEach((technology) => params.append("technologies", technology));
  if (filters.domain) {
    params.set("domain", filters.domain);
  }
  if (filters.category) {
    params.set("category", filters.category);
  }
  if (filters.sourceDomain) {
    params.set("source_domain", filters.sourceDomain);
  }
  if (filters.language) {
    params.set("language", filters.language);
  }
  if (filters.registeredFrom) {
    params.set("registered_from", filters.registeredFrom);
  }
  if (filters.registeredTo) {
    params.set("registered_to", filters.registeredTo);
  }
  if (filters.isPrimarySource !== null) {
    params.set("is_primary_source", String(filters.isPrimarySource));
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
export function jstDateToRegisteredFromIso(dateString: string): string {
  return new Date(`${dateString}T00:00:00.000${JST_OFFSET}`).toISOString();
}

/** `<input type="date">` の値を、JST のその日 23:59:59.999 を表す UTC の ISO8601 文字列へ変換する（期間上限用）。 */
export function jstDateToRegisteredToIso(dateString: string): string {
  return new Date(`${dateString}T23:59:59.999${JST_OFFSET}`).toISOString();
}

/**
 * ISO8601 文字列（UTC）を JST の日付に直し、`<input type="date">` へ渡せる `YYYY-MM-DD` にする。
 *
 * `isoString` が不正で `Date` としてパースできない場合は空文字列を返す。呼び出し元
 * （`ArticleFilterPanel` の `defaultValue`）はレンダー中にこれを呼ぶため、
 * `.toISOString()` の `RangeError` をここで防がないとページ全体がクラッシュする
 * （`parseArticleFiltersFromSearchParams` 側で不正値を弾いていても、防御的にここでも弾く）。
 */
export function isoToJstDateInputValue(isoString: string): string {
  const utcMillis = new Date(isoString).getTime();
  if (Number.isNaN(utcMillis)) {
    return "";
  }
  const jstMillis = utcMillis + 9 * 60 * 60 * 1000;
  return new Date(jstMillis).toISOString().slice(0, 10);
}

interface ListInterestArticlesOptions {
  /** 1始まりのページ番号。省略時は backend の既定値（1）。 */
  page?: number;
  limit?: number;
}

/**
 * 関心記事一覧を取得する（`PROJECT_SPEC.md` §6.3）。
 *
 * ページングは番号付き（`page` / `limit`）。Issue #91 以前は cursor 方式だったが、
 * 目的のページへ直接移動できるようにするため置き換えた（`getFeed` と同じ形）。
 */
export function listInterestArticles(
  filters: ArticleFilters,
  { page, limit }: ListInterestArticlesOptions = {},
): Promise<InterestArticleListResponse> {
  const query = buildSearchParamsFromFilters(filters);
  if (page !== undefined) {
    query.set("page", String(page));
  }
  if (limit !== undefined) {
    query.set("limit", String(limit));
  }

  const queryString = query.toString();
  return apiFetch<InterestArticleListResponse>(
    `/api/articles${queryString ? `?${queryString}` : ""}`,
  );
}

/**
 * 記事を関心記事一覧から除外する。`user_articles` の該当行を削除するだけで
 * `article_feedback` には触れない（Issue #14 ヒアリング済み決定事項）。
 */
export function deleteInterestArticle(articleId: string): Promise<void> {
  return apiFetch<void>(`/api/articles/${encodeURIComponent(articleId)}/interest`, {
    method: "DELETE",
  });
}
