"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import { ArticleCard } from "@/components/features/ArticleCard";
import { FeedFilterPanel } from "@/components/features/FeedFilterPanel";
import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { Pagination } from "@/components/ui/Pagination";
import { useFeed } from "@/hooks/useFeed";
import { FIRST_FEED_PAGE, parseFeedFiltersFromSearchParams, parseFeedPageOrFirst } from "@/lib/feed";

/**
 * Discover フィード本体（`PROJECT_SPEC.md` §13.2、検索・絞り込み・ページングは Issue #90）。
 *
 * フィルターは URL クエリから読む。`FeedFilterPanel` と同じ
 * `parseFeedFiltersFromSearchParams` を使うため、フィルター条件をコンポーネント間で
 * 二重管理することにならない（URL が唯一の情報源、`InterestArticleList` と同じ設計）。
 *
 * 番号付きページャ（`Pagination`）へ書き換え、無限スクロール（IntersectionObserver）
 * は撤去した。ページ番号もフィルターと同じく URL クエリを唯一の情報源にする
 * （Issue #95）。`useFeed` は URL から読んだページ番号を受け取るだけで、自分では
 * 保持しない。
 */
export function DiscoverFeed() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const filters = useMemo(() => parseFeedFiltersFromSearchParams(searchParams), [searchParams]);
  const page = parseFeedPageOrFirst(searchParams.get("page"));

  // ページ移動は履歴へ積む（`push`）。フィルターの変更が `router.replace` なのと
  // あえて分けている。3ページ目から戻ったら2ページ目へ帰るのが、ページャを操作した
  // 側の期待だと判断した（Issue #95）。
  //
  // `scroll: false` を付けるのは、書き換え前（ローカル state の更新）と同じく
  // スクロール位置を動かさないため。`push` の既定は先頭へスクロールする。
  const handlePageChange = useCallback(
    (nextPage: number) => {
      if (nextPage === page) {
        // 今見ているページ番号のボタンは押せる（`Pagination` が無効化するのは
        // 「前へ」「次へ」だけ）。ここで弾かないと、URL が変わらないまま履歴だけが
        // 積まれ、戻るボタンを余計に押さないと前のページへ帰れなくなる。
        // 書き換え前は `useFeed` の `setPage` が同じガードを持っていた。
        return;
      }

      const params = new URLSearchParams(searchParams);
      if (nextPage === FIRST_FEED_PAGE) {
        // 1ページ目では `page` を URL に出さない（既定値を URL に出さないのは
        // `buildSearchParamsFromFilters` の他の条件と同じ扱い）。
        params.delete("page");
      } else {
        params.set("page", String(nextPage));
      }
      const query = params.toString();
      router.push(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [page, pathname, router, searchParams],
  );

  const { items, isLoading, error, totalPages, totalCount, applyFeedback, removeFeedback } =
    useFeed(filters, page);

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
            onPageChange={handlePageChange}
          />
        </>
      )}
    </section>
  );
}
