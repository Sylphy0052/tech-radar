"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { FormEvent } from "react";

import {
  buildSearchParamsFromFilters,
  INTEREST_ARTICLE_ORIGINS,
  isoToJstDateInputValue,
  jstDateToRegisteredFromIso,
  jstDateToRegisteredToIso,
  ORIGIN_LABELS,
  parseArticleFiltersFromSearchParams,
} from "@/lib/interest-articles";
import type { ArticleFilters } from "@/lib/interest-articles";

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
 * カンマ区切りの自由入力を配列へ変換する。空要素（連続カンマ・前後の空白のみ）は捨てる
 * （`FeedFilterPanel` と同じ）。
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

function isPrimarySourceToSelectValue(value: boolean | null): string {
  if (value === null) {
    return "";
  }
  return value ? "true" : "false";
}

function parseIsPrimarySourceSelection(value: FormDataEntryValue | null): boolean | null {
  if (value === "true") {
    return true;
  }
  if (value === "false") {
    return false;
  }
  return null;
}

/**
 * 関心記事一覧のフィルター UI（`PROJECT_SPEC.md` §6.3）。
 *
 * URL クエリだけを唯一の状態源にする。フォーム自体はコンポーネント内 state を
 * 持たず、`key={searchParams.toString()}` を付けた非制御コンポーネントにして
 * いる。こうすると入力中の再レンダリングでは表示値が保持されたまま、URL が
 * 外部要因（ブラウザの戻る/進む・`InterestArticleList` 側からの遷移等）で
 * 変わったときだけフォームが作り直されて最新の URL の値を表示する。
 *
 * 検索語・トピック・技術タグは Issue #91 で追加した。トピックと技術タグは
 * `FeedFilterPanel` と同じくカンマ区切りの自由入力で複数指定を受け付ける
 * （backend では「指定した全てを含む」AND 条件になる）。
 *
 * ジャンル（domain/category）・情報源・言語は固定の選択肢を持たない自由入力に
 * している。これらは LLM が記事ごとに分類した自由文字列（`analysis/prompt.py`）
 * であり、backend にも列挙された一覧は存在しない。実在する値を選択式にするには
 * 専用の集計 API が要るが、backend 変更は本タスクの対象外のため、まずは自由入力
 * とした（Issue #14 ヒアリング回答）。
 */
export function ArticleFilterPanel() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const filters = parseArticleFiltersFromSearchParams(searchParams);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);

    const registeredFromDate = formData.get("registered_from_date");
    const registeredToDate = formData.get("registered_to_date");

    const nextFilters: ArticleFilters = {
      origin: formData.getAll("origin").map(String) as ArticleFilters["origin"],
      q: emptyToNull(formData.get("q")),
      topics: parseCommaSeparatedList(formData.get("topics")),
      technologies: parseCommaSeparatedList(formData.get("technologies")),
      domain: emptyToNull(formData.get("domain")),
      category: emptyToNull(formData.get("category")),
      sourceDomain: emptyToNull(formData.get("source_domain")),
      language: emptyToNull(formData.get("language")),
      registeredFrom:
        typeof registeredFromDate === "string" && registeredFromDate !== ""
          ? jstDateToRegisteredFromIso(registeredFromDate)
          : null,
      registeredTo:
        typeof registeredToDate === "string" && registeredToDate !== ""
          ? jstDateToRegisteredToIso(registeredToDate)
          : null,
      isPrimarySource: parseIsPrimarySourceSelection(formData.get("is_primary_source")),
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
      aria-label="関心記事のフィルター"
      className="panel flex flex-col gap-4 text-sm"
    >
      <h2 className="heading text-sm">フィルター</h2>

      <fieldset className="flex flex-col gap-2">
        <legend className="mono-label">登録方法</legend>
        {INTEREST_ARTICLE_ORIGINS.map((origin) => (
          <label
            key={origin}
            className="flex items-center gap-2 text-ink has-checked:text-accent-strong"
          >
            <input
              type="checkbox"
              name="origin"
              value={origin}
              defaultChecked={filters.origin.includes(origin)}
            />
            {ORIGIN_LABELS[origin]}
          </label>
        ))}
      </fieldset>

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

      <div className={FIELD_CLASS}>
        <label className={FIELD_CLASS}>
          {/* 補足はラベルの外に置く。ラベル内に入れるとアクセシブル名が説明文まで
              含んだ長い文字列になり、支援技術での読み上げも冗長になる
              （`FeedFilterPanel` の対象期間と同じ扱い）。 */}
          <span className="mono-label">技術タグ（カンマ区切りで複数指定可）</span>
          <input
            type="text"
            name="technologies"
            defaultValue={filters.technologies.join(", ")}
            className="field-input"
          />
        </label>
        <span className="text-xs text-ink-subtle">
          技術タグは一覧のカードには出ないが、絞り込みには使える
        </span>
      </div>

      <label className={FIELD_CLASS}>
        <span className="mono-label">ジャンル（大分類）</span>
        <input type="text" name="domain" defaultValue={filters.domain ?? ""} className="field-input" />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">ジャンル（中分類）</span>
        <input
          type="text"
          name="category"
          defaultValue={filters.category ?? ""}
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

      <label className={FIELD_CLASS}>
        <span className="mono-label">言語</span>
        <input
          type="text"
          name="language"
          defaultValue={filters.language ?? ""}
          className="field-input"
        />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">登録日時（開始）</span>
        <input
          type="date"
          name="registered_from_date"
          defaultValue={filters.registeredFrom ? isoToJstDateInputValue(filters.registeredFrom) : ""}
          className="field-input"
        />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">登録日時（終了）</span>
        <input
          type="date"
          name="registered_to_date"
          defaultValue={filters.registeredTo ? isoToJstDateInputValue(filters.registeredTo) : ""}
          className="field-input"
        />
      </label>

      <label className={FIELD_CLASS}>
        <span className="mono-label">公式 / 非公式</span>
        <select
          name="is_primary_source"
          defaultValue={isPrimarySourceToSelectValue(filters.isPrimarySource)}
          className="field-input"
        >
          <option value="">すべて</option>
          <option value="true">公式・一次情報のみ</option>
          <option value="false">非公式のみ</option>
        </select>
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
