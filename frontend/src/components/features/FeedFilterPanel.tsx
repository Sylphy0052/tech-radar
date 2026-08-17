"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { FormEvent } from "react";

import {
  MAX_FEED_MAX_AGE_DAYS,
  MIN_FEED_MAX_AGE_DAYS,
  buildSearchParamsFromFilters,
  isoToJstDateInputValue,
  jstDateToPublishedFromIso,
  jstDateToPublishedToIso,
  parseFeedFiltersFromSearchParams,
  parseMaxAgeDaysOrNull,
} from "@/lib/feed";
import type { FeedFilters } from "@/lib/feed";

/** ラベルと入力欄を縦に並べる、キー/値ペアの基本レイアウト。 */
const FIELD_CLASS = "flex flex-col gap-1";

function emptyToNull(value: FormDataEntryValue | null): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/**
 * カンマ区切りの自由入力を配列へ変換する。空要素（連続カンマ・前後の空白のみ）は捨てる。
 */
function parseCommaSeparatedList(value: FormDataEntryValue | null): string[] {
  if (typeof value !== "string") {
    return [];
  }
  return value
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/**
 * 対象期間の入力値を日数へ変換する。空欄・整数でない値・範囲外は `null`
 * （backend の既定に任せる）へ落とす。`<input type="number">` は min/max を
 * 付けても手入力やブラウザによっては範囲外の値を送れるため、ここでも確かめる。
 *
 * 判定そのものは URL から復元するときと同じ `parseMaxAgeDaysOrNull` に任せ、
 * ここは `FormData` の値を文字列へ均すだけにする（境界値を二重に持たない）。
 */
function parseMaxAgeDaysField(value: FormDataEntryValue | null): number | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return parseMaxAgeDaysOrNull(trimmed === "" ? null : trimmed);
}

/**
 * Discover フィードのフィルター UI（Issue #90、`PROJECT_SPEC.md` §13.2）。
 *
 * `ArticleFilterPanel`（関心記事一覧向け）と同じ設計を踏襲する。URL クエリだけを
 * 唯一の状態源にし、フォーム自体はコンポーネント内 state を持たず、
 * `key={searchParams.toString()}` を付けた非制御コンポーネントにしている。
 * こうすると入力中の再レンダリングでは表示値が保持されたまま、URL が外部要因
 * （ブラウザの戻る/進む等）で変わったときだけフォームが作り直されて最新の URL
 * の値を表示する。
 *
 * トピック・技術タグは LLM が記事ごとに分類した自由文字列で、backend にも
 * 列挙された一覧が存在しないため、カンマ区切りの自由入力で複数指定を受け付ける
 * （`ArticleFilterPanel` がジャンル等を自由入力にしているのと同じ理由）。
 */
export function FeedFilterPanel() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const filters = parseFeedFiltersFromSearchParams(searchParams);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);

    const publishedFromDate = formData.get("published_from_date");
    const publishedToDate = formData.get("published_to_date");

    const nextFilters: FeedFilters = {
      q: emptyToNull(formData.get("q")),
      topics: parseCommaSeparatedList(formData.get("topics")),
      technologies: parseCommaSeparatedList(formData.get("technologies")),
      publishedFrom:
        typeof publishedFromDate === "string" && publishedFromDate !== ""
          ? jstDateToPublishedFromIso(publishedFromDate)
          : null,
      publishedTo:
        typeof publishedToDate === "string" && publishedToDate !== ""
          ? jstDateToPublishedToIso(publishedToDate)
          : null,
      sourceDomain: emptyToNull(formData.get("source_domain")),
      maxAgeDays: parseMaxAgeDaysField(formData.get("max_age_days")),
    };

    const query = buildSearchParamsFromFilters(nextFilters).toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  function handleReset(): void {
    router.replace(pathname, { scroll: false });
  }

  return (
    <form
      key={searchParams.toString()}
      onSubmit={handleSubmit}
      aria-label="フィードのフィルター"
      className="panel flex flex-col gap-4 text-sm"
    >
      <h2 className="heading text-sm">フィルター</h2>

      <label className={FIELD_CLASS}>
        <span className="mono-label">検索語</span>
        <input type="text" name="q" defaultValue={filters.q ?? ""} className="field-input" />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">トピック（カンマ区切りで複数指定可）</span>
        <input
          type="text"
          name="topics"
          defaultValue={filters.topics.join(", ")}
          className="field-input"
        />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">技術タグ（カンマ区切りで複数指定可）</span>
        <input
          type="text"
          name="technologies"
          defaultValue={filters.technologies.join(", ")}
          className="field-input"
        />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">情報源</span>
        <input
          type="text"
          name="source_domain"
          defaultValue={filters.sourceDomain ?? ""}
          className="field-input"
        />
      </label>

      <div className={FIELD_CLASS}>
        <label className={FIELD_CLASS}>
          {/* 補足はラベルの外に置く。ラベル内に入れるとアクセシブル名が説明文まで
              含んだ長い文字列になり、支援技術での読み上げも冗長になる。 */}
          <span className="mono-label">
            対象期間（{MIN_FEED_MAX_AGE_DAYS}〜{MAX_FEED_MAX_AGE_DAYS}日）
          </span>
          <input
            type="number"
            name="max_age_days"
            min={MIN_FEED_MAX_AGE_DAYS}
            max={MAX_FEED_MAX_AGE_DAYS}
            step={1}
            defaultValue={filters.maxAgeDays ?? ""}
            className="field-input"
          />
        </label>
        <span className="text-xs text-ink-subtle">
          何日前までの記事を対象にするか。未指定は7日。公開日の指定はこの期間の内側にだけ効く
        </span>
      </div>

      <label className={FIELD_CLASS}>
        <span className="mono-label">公開日（開始）</span>
        <input
          type="date"
          name="published_from_date"
          defaultValue={filters.publishedFrom ? isoToJstDateInputValue(filters.publishedFrom) : ""}
          className="field-input"
        />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">公開日（終了）</span>
        <input
          type="date"
          name="published_to_date"
          defaultValue={filters.publishedTo ? isoToJstDateInputValue(filters.publishedTo) : ""}
          className="field-input"
        />
      </label>

      <div className="flex gap-2 pt-1">
        <button type="submit" className="btn btn-primary">
          絞り込む
        </button>
        <button type="button" onClick={handleReset} className="btn">
          クリア
        </button>
      </div>
    </form>
  );
}
