"use client";

import { useSearchParams } from "next/navigation";
import { useMemo } from "react";

import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { useInterestArticles } from "@/hooks/useInterestArticles";
import { formatDateTimeJa } from "@/lib/format-date";
import {
  CONTENT_TYPE_LABELS,
  originLabel,
  parseArticleFiltersFromSearchParams,
} from "@/lib/interest-articles";
import type { InterestArticleItem } from "@/lib/interest-articles";
import { isSafeHttpUrl } from "@/lib/safe-url";

function contentTypeLabel(contentType: string | null): string | null {
  if (contentType === null) {
    return null;
  }
  return CONTENT_TYPE_LABELS[contentType] ?? contentType;
}

interface InterestArticleCardProps {
  item: InterestArticleItem;
  onRemove: () => void;
}

/** 記事1件分のカード。関心記事一覧の表示項目（`PROJECT_SPEC.md` §6.3）をまとめる。 */
function InterestArticleCard({ item, onRemove }: InterestArticleCardProps) {
  const contentType = contentTypeLabel(item.content_type);

  return (
    <article className="flex flex-col gap-2 rounded border border-zinc-200 p-4 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
        <span className="rounded-full bg-zinc-100 px-2 py-0.5 dark:bg-zinc-800">
          {originLabel(item.origin)}
        </span>
        {item.is_primary_source && (
          <span className="rounded-full bg-blue-100 px-2 py-0.5 text-blue-700 dark:bg-blue-950 dark:text-blue-300">
            公式・一次情報
          </span>
        )}
        <span>{formatDateTimeJa(item.registered_at)}</span>
        <span>{item.source_domain}</span>
        {contentType !== null && <span>{contentType}</span>}
      </div>

      <h3 className="text-base font-semibold">{item.title}</h3>
      {item.translated_title !== null && (
        <p className="text-sm text-zinc-700 dark:text-zinc-300">{item.translated_title}</p>
      )}

      {item.topics.length > 0 && (
        <div className="flex flex-wrap items-center gap-1 text-xs">
          <span className="text-zinc-500 dark:text-zinc-400">トピック:</span>
          {item.topics.map((topic) => (
            <span key={topic} className="rounded-full bg-zinc-100 px-2 py-0.5 dark:bg-zinc-800">
              {topic}
            </span>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onRemove}
          className="text-xs text-zinc-500 underline dark:text-zinc-400"
        >
          関心対象から除外
        </button>
        {isSafeHttpUrl(item.original_url) ? (
          <a
            href={item.original_url}
            target="_blank"
            rel="noreferrer noopener"
            className="ml-auto text-sm underline"
          >
            元記事を開く
          </a>
        ) : (
          <span className="ml-auto text-sm text-zinc-400 dark:text-zinc-500">
            元記事のリンクを表示できません
          </span>
        )}
      </div>
    </article>
  );
}

/**
 * 関心記事一覧本体（`PROJECT_SPEC.md` §6.3）。
 *
 * フィルターは URL クエリから読む。`ArticleFilterPanel` と同じ
 * `parseArticleFiltersFromSearchParams` を使うため、フィルター条件を
 * コンポーネント間で二重管理することにならない（URL が唯一の情報源）。
 */
export function InterestArticleList() {
  const searchParams = useSearchParams();
  const filters = useMemo(() => parseArticleFiltersFromSearchParams(searchParams), [searchParams]);

  const { items, isLoading, isLoadingMore, error, hasMore, loadMore, removeArticle } =
    useInterestArticles(filters);

  if (isLoading) {
    return <LoadingIndicator label="読み込み中..." />;
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">関心記事一覧</h2>
      {error !== null && <ErrorMessage message={error} />}

      {items.length > 0 && (
        <div className="flex flex-col gap-4">
          {items.map((item) => (
            <InterestArticleCard
              key={item.article_id}
              item={item}
              onRemove={() => removeArticle(item.article_id)}
            />
          ))}
        </div>
      )}

      {items.length === 0 && !hasMore && (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">該当する記事がありません。</p>
      )}

      {hasMore && (
        <div className="flex flex-col items-center gap-2 py-2">
          {isLoadingMore ? (
            <LoadingIndicator label="さらに読み込み中..." />
          ) : (
            <button
              type="button"
              onClick={loadMore}
              className="rounded border border-zinc-300 px-4 py-2 text-sm dark:border-zinc-700"
            >
              さらに読み込む
            </button>
          )}
        </div>
      )}
    </section>
  );
}
