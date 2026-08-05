"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  buildSearchParamsFromFilters,
  deleteInterestArticle,
  listInterestArticles,
} from "@/lib/interest-articles";
import type { ArticleFilters, InterestArticleItem } from "@/lib/interest-articles";
import { reportUnexpectedState } from "@/lib/report-unexpected";
import { getRequestErrorMessage } from "@/lib/request-error-message";

interface UseInterestArticlesResult {
  items: InterestArticleItem[];
  isLoading: boolean;
  isLoadingMore: boolean;
  error: string | null;
  /** next_cursor が null になったら false。以降 loadMore は何もしない。 */
  hasMore: boolean;
  loadMore: () => void;
  /** 記事を関心記事一覧から除外する（`DELETE /api/articles/{article_id}/interest`）。 */
  removeArticle: (articleId: string) => void;
}

/** 既存項目と新規項目を article_id で重複排除しながら連結する（`useFeed.mergeItems` と同じ狙い）。 */
function mergeItems(current: InterestArticleItem[], incoming: InterestArticleItem[]): InterestArticleItem[] {
  const seen = new Set(current.map((item) => item.article_id));
  const deduped = incoming.filter((item) => !seen.has(item.article_id));
  return [...current, ...deduped];
}

/** 指定 article_id を除いた新しい配列を返す。 */
function withoutArticle(items: InterestArticleItem[], articleId: string): InterestArticleItem[] {
  return items.filter((item) => item.article_id !== articleId);
}

/**
 * 関心記事一覧の状態管理 hook（`useFeed` と同じ構成）。
 *
 * フィルター条件が変わるたびに一覧を取得し直す（先頭ページから。カーソルは
 * フィルターごとに別の並びを指すため使い回せない）。除外操作は `useFeed` の
 * `removeFeedback` と同じく、先にローカル state から消してから API を呼び、
 * 失敗時はもとの位置へ戻す。
 *
 * `loadMore` は `useFeed` と異なり毎レンダーで参照が変わりうる（`nextCursor` /
 * `filters` に依存するため）。`InterestArticleList` はボタン押下でしか呼ばない
 * ため（IntersectionObserver を使わない）、参照の安定性は要らない。
 *
 * `filters` は呼び出し側（`InterestArticleList`）が URL から `useMemo` で
 * 導出したオブジェクトを渡す想定。参照が安定していれば無駄な再取得は起きない
 * （内部的にはクエリ文字列で比較するため、安定していなくても正しく動く）。
 */
export function useInterestArticles(filters: ArticleFilters): UseInterestArticlesResult {
  const [items, setItems] = useState<InterestArticleItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isLoadingMoreRef = useRef(false);

  // 送信中（pending）の article_id 集合。連打で二重に DELETE を送らないためのガード
  // （`useFeed.pendingArticleIdsRef` と同じ狙い）。
  const pendingArticleIdsRef = useRef<Set<string>>(new Set());

  const isMountedRef = useRef(true);
  useEffect(
    () => () => {
      isMountedRef.current = false;
    },
    [],
  );

  // フィルターが変わったかどうかを「レンダー中に前回値と比較して直す」React の
  // 公式パターンで検出する（https://react.dev/learn/you-might-not-need-an-effect
  // の "Adjusting state when a prop changes"）。`useEffect` の本体で setState を
  // 同期的に呼ぶとカスケード再レンダーになるため（react-hooks/set-state-in-effect
  // が検出する）、リセットはレンダー中のこちらへ寄せ、`useEffect` 側は非同期の
  // fetch とその結果を反映する setState だけにする。オブジェクト参照ではなく
  // クエリ文字列で比較するので、呼び出し側が毎レンダー新しい `filters`
  // オブジェクトを渡しても（値が同じなら）無駄なリセットは起きない。
  const filterKey = buildSearchParamsFromFilters(filters).toString();
  const [previousFilterKey, setPreviousFilterKey] = useState(filterKey);
  if (filterKey !== previousFilterKey) {
    setPreviousFilterKey(filterKey);
    setItems([]);
    setNextCursor(null);
    setIsLoading(true);
    setError(null);
  }

  // `loadMore` 発行時点の filterKey を捕捉するための最新値 ref。
  // フィルターが切り替わった後に旧フィルターの loadMore レスポンスが解決しても、
  // このref越しに世代のずれを検出して破棄できるようにする。
  const filterKeyRef = useRef(filterKey);
  useEffect(() => {
    filterKeyRef.current = filterKey;
  }, [filterKey]);

  useEffect(() => {
    let cancelled = false;

    listInterestArticles(filters)
      .then((response) => {
        if (cancelled) {
          return;
        }
        setItems(response.items);
        setNextCursor(response.next_cursor);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) {
          return;
        }
        setError(getRequestErrorMessage(err));
      })
      .finally(() => {
        if (cancelled) {
          return;
        }
        setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [filters]);

  const loadMore = useCallback(() => {
    if (nextCursor === null || isLoadingMoreRef.current) {
      return;
    }
    const cursor = nextCursor;
    // 発行時点のフィルターを捕捉する。レスポンス到達時にこれと食い違っていれば、
    // その間にフィルターが切り替わっており（初回取得側の `cancelled` と同じ狙い）、
    // 新フィルターの一覧へ旧フィルターの記事を混ぜてしまうため破棄する。
    const requestFilterKey = filterKeyRef.current;
    const isStaleResponse = () =>
      !isMountedRef.current || filterKeyRef.current !== requestFilterKey;
    isLoadingMoreRef.current = true;
    setIsLoadingMore(true);
    listInterestArticles(filters, { cursor })
      .then((response) => {
        if (isStaleResponse()) {
          return;
        }
        setItems((current) => mergeItems(current, response.items));
        setNextCursor(response.next_cursor);
        setError(null);
      })
      .catch((err: unknown) => {
        if (isStaleResponse()) {
          return;
        }
        setError(getRequestErrorMessage(err));
      })
      .finally(() => {
        isLoadingMoreRef.current = false;
        if (isStaleResponse()) {
          return;
        }
        setIsLoadingMore(false);
      });
  }, [nextCursor, filters]);

  const removeArticle = useCallback(
    (articleId: string) => {
      if (pendingArticleIdsRef.current.has(articleId)) {
        return;
      }

      const index = items.findIndex((item) => item.article_id === articleId);
      if (index === -1) {
        // 画面に出ている記事の操作なら、そのレンダーの items に必ず含まれる。
        // ここへ来るのは一覧に無い article_id を渡されたときだけで、握り潰すと
        // 「押しても何も起きない」に見える（Issue #45）。本番の表示は変えずに
        // 開発時だけ痕跡を残す。
        reportUnexpectedState(`removeArticle: 一覧に無いarticle_id: ${articleId}`);
        return;
      }
      const removedItem = items[index];

      setItems((current) => withoutArticle(current, articleId));
      pendingArticleIdsRef.current.add(articleId);

      deleteInterestArticle(articleId)
        .then(() => {
          if (!isMountedRef.current) {
            return;
          }
          setError(null);
        })
        .catch((err: unknown) => {
          if (!isMountedRef.current) {
            return;
          }
          setItems((current) => {
            const restored = [...current];
            restored.splice(index, 0, removedItem);
            return restored;
          });
          setError(getRequestErrorMessage(err));
        })
        .finally(() => {
          pendingArticleIdsRef.current.delete(articleId);
        });
    },
    [items],
  );

  return {
    items,
    isLoading,
    isLoadingMore,
    error,
    hasMore: nextCursor !== null,
    loadMore,
    removeArticle,
  };
}
