"use client";

import { useSearchParams } from "next/navigation";
import { useMemo } from "react";

import { ArticleCard } from "@/components/features/ArticleCard";
import { FeedFilterPanel } from "@/components/features/FeedFilterPanel";
import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { Pagination } from "@/components/ui/Pagination";
import { useFeed } from "@/hooks/useFeed";
import { parseFeedFiltersFromSearchParams } from "@/lib/feed";

/**
 * Discover フィード本体（`PROJECT_SPEC.md` §13.2、検索・絞り込み・ページングは Issue #90）。
 *
 * フィルターは URL クエリから読む。`FeedFilterPanel` と同じ
 * `parseFeedFiltersFromSearchParams` を使うため、フィルター条件をコンポーネント間で
 * 二重管理することにならない（URL が唯一の情報源、`InterestArticleList` と同じ設計）。
 *
 * 番号付きページャ（`Pagination`）へ書き換え、無限スクロール（IntersectionObserver）
 * は撤去した。
 */
export function DiscoverFeed() {
  const searchParams = useSearchParams();
  const filters = useMemo(() => parseFeedFiltersFromSearchParams(searchParams), [searchParams]);

  const { items, isLoading, error, page, totalPages, totalCount, setPage, applyFeedback, removeFeedback } =
    useFeed(filters);

  return (
    <section className="flex flex-col gap-4">
      <h2 className="heading text-lg">おすすめ記事</h2>
      <FeedFilterPanel />

      {isLoading ? (
        <LoadingIndicator label="読み込み中..." />
      ) : (
        <>
          {error !== null && <ErrorMessage message={error} />}

          {items.length > 0 && (
            <div className="flex flex-col gap-4">
              {items.map((item) => (
                <ArticleCard
                  key={item.article_id}
                  item={item}
                  onFeedback={(action, reason) => applyFeedback(item.article_id, action, reason)}
                  onRemoveFeedback={() => removeFeedback(item.article_id)}
                />
              ))}
            </div>
          )}

          {items.length === 0 && error === null && (
            // 総件数が 0 なら条件に当たる記事が無く、総件数があるのに空なら
            // 範囲外のページを見ている（絞り込みで件数が減った直後のリロード等）。
            // どちらも同じ文言だと、他のページに記事があることが伝わらない。
            <p className="font-mono text-sm text-ink-subtle">
              {totalCount === 0
                ? "条件に当たる記事がありません。"
                : "このページには記事がありません。他のページを開いてください。"}
            </p>
          )}

          <Pagination
            currentPage={page}
            totalPages={totalPages}
            totalCount={totalCount}
            onPageChange={setPage}
          />
        </>
      )}
    </section>
  );
}
