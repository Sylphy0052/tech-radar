"use client";

import { useEffect, useRef } from "react";

import { ArticleCard } from "@/components/features/ArticleCard";
import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { useFeed } from "@/hooks/useFeed";

/**
 * Discover フィード本体（`PROJECT_SPEC.md` §6.1）。
 *
 * 一覧末尾のセンチネル要素を IntersectionObserver で監視し、可視になったら
 * `loadMore()` を呼んで次ページを追記する。IntersectionObserver が無い環境
 * （テスト環境や古いブラウザ）でも読み進められるよう、「さらに読み込む」
 * ボタンを常に併置する（キーボード操作でも次ページを読めるようにするため）。
 */
export function DiscoverFeed() {
  const {
    items,
    isLoading,
    isLoadingMore,
    error,
    hasMore,
    loadMore,
    applyFeedback,
    removeFeedback,
  } = useFeed();

  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!hasMore || typeof IntersectionObserver === "undefined") {
      return;
    }
    const sentinel = sentinelRef.current;
    if (!sentinel) {
      return;
    }

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        loadMore();
      }
    });
    observer.observe(sentinel);

    return () => {
      observer.disconnect();
    };
  }, [hasMore, loadMore]);

  if (isLoading) {
    return <LoadingIndicator label="読み込み中..." />;
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">おすすめ記事</h2>
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

      {/* 空表示に切り替えるのは次ページが無いと分かってから。ページ内が全件 Bad
          だと items が空でも next_cursor は返る（バックエンドは cursor を除外前の
          rank から計算する）ため、items が空というだけで打ち切ると読み進める手段を
          失う。 */}
      {items.length === 0 && !hasMore && (
        <p className="text-sm text-zinc-500 dark:text-zinc-400">表示できる記事がありません。</p>
      )}

      {hasMore ? (
        <div ref={sentinelRef} className="flex flex-col items-center gap-2 py-2">
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
      ) : (
        items.length > 0 && (
          <p className="text-center text-sm text-zinc-500 dark:text-zinc-400">
            すべての記事を読み込みました
          </p>
        )
      )}
    </section>
  );
}
