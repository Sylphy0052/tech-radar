"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getFeed } from "@/lib/feed";
import type { FeedItem } from "@/lib/feed";
import { deleteFeedback, sendFeedback } from "@/lib/feedback";
import type { ArticleFeedback, BadReason, FeedbackAction } from "@/lib/feedback";
import { getRequestErrorMessage } from "@/lib/request-error-message";

interface UseFeedResult {
  items: FeedItem[];
  isLoading: boolean;
  isLoadingMore: boolean;
  error: string | null;
  /** next_cursor が null になったら false。以降 loadMore は何もしない。 */
  hasMore: boolean;
  loadMore: () => void;
  applyFeedback: (articleId: string, action: FeedbackAction, reason?: BadReason) => void;
  removeFeedback: (articleId: string) => void;
}

/** 既存項目と新規項目を article_id で重複排除しながら連結する。 */
function mergeItems(current: FeedItem[], incoming: FeedItem[]): FeedItem[] {
  const seen = new Set(current.map((item) => item.article_id));
  const deduped = incoming.filter((item) => !seen.has(item.article_id));
  return [...current, ...deduped];
}

/** 指定 article_id の項目だけ feedback を書き換えた新しい配列を返す。 */
function withFeedback(
  items: FeedItem[],
  articleId: string,
  feedback: ArticleFeedback | null,
): FeedItem[] {
  return items.map((item) => (item.article_id === articleId ? { ...item, feedback } : item));
}

/**
 * Discover フィードの状態管理 hook。
 *
 * 初回ロード・カーソルベースの追加ロード・フィードバックの楽観的更新を担う。
 * フィードバックは先にローカル state を書き換えてから API を呼び、失敗時は
 * 呼び出し前の状態へロールバックする（ボタン押下への即時反応を優先するため）。
 *
 * 最新の items は `itemsRef` 経由で参照する。`setState` の関数形式アップデータ内で
 * API 呼び出しのような副作用を行うと、React が purity チェックのためアップデータを
 * 二重に呼ぶ場合に副作用も二重発火してしまうため、読み取りと更新を分離している
 * （`usePolling` の `fetchFnRef` と同じ狙い）。
 */
export function useFeed(): UseFeedResult {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const itemsRef = useRef<FeedItem[]>(items);
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  // アンマウント後に非同期結果で setState してしまわないためのガード。
  const isMountedRef = useRef(true);
  useEffect(
    () => () => {
      isMountedRef.current = false;
    },
    [],
  );

  useEffect(() => {
    // isLoading は useState(true) の初期値をそのまま使う。この effect は
    // 空の依存配列で初回マウント時にしか走らないため、ここで改めて true に
    // し直す必要はない（するとレンダー中の setState として lint に弾かれる）。
    let cancelled = false;
    getFeed()
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
  }, []);

  const loadMore = useCallback(() => {
    if (nextCursor === null || isLoadingMore) {
      return;
    }
    setIsLoadingMore(true);
    getFeed({ cursor: nextCursor })
      .then((response) => {
        if (!isMountedRef.current) {
          return;
        }
        setItems((current) => mergeItems(current, response.items));
        setNextCursor(response.next_cursor);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!isMountedRef.current) {
          return;
        }
        setError(getRequestErrorMessage(err));
      })
      .finally(() => {
        if (!isMountedRef.current) {
          return;
        }
        setIsLoadingMore(false);
      });
  }, [nextCursor, isLoadingMore]);

  const applyFeedback = useCallback(
    (articleId: string, action: FeedbackAction, reason?: BadReason) => {
      const target = itemsRef.current.find((item) => item.article_id === articleId);
      if (!target) {
        return;
      }

      const previousFeedback = target.feedback;
      // 同じ action を押し直したときはトグルで取り消しにする。ただし理由を
      // 明示して送り直した場合（Bad の理由を選び直したとき）は取り消しではなく
      // 更新として扱う。
      const isToggleOff =
        previousFeedback !== null && previousFeedback.action === action && reason === undefined;
      const optimisticFeedback: ArticleFeedback | null = isToggleOff
        ? null
        : { action, reason: reason ?? null, created_at: new Date().toISOString() };

      setItems((current) => withFeedback(current, articleId, optimisticFeedback));

      const request = isToggleOff
        ? deleteFeedback(articleId).then((): ArticleFeedback | null => null)
        : sendFeedback(articleId, { action, reason });

      request
        .then((result) => {
          if (!isMountedRef.current) {
            return;
          }
          setItems((current) => withFeedback(current, articleId, result));
          setError(null);
        })
        .catch((err: unknown) => {
          if (!isMountedRef.current) {
            return;
          }
          setItems((current) => withFeedback(current, articleId, previousFeedback));
          setError(getRequestErrorMessage(err));
        });
    },
    [],
  );

  const removeFeedback = useCallback((articleId: string) => {
    const target = itemsRef.current.find((item) => item.article_id === articleId);
    if (!target || target.feedback === null) {
      return;
    }
    const previousFeedback = target.feedback;

    setItems((current) => withFeedback(current, articleId, null));

    deleteFeedback(articleId)
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
        setItems((current) => withFeedback(current, articleId, previousFeedback));
        setError(getRequestErrorMessage(err));
      });
  }, []);

  return {
    items,
    isLoading,
    isLoadingMore,
    error,
    hasMore: nextCursor !== null,
    loadMore,
    applyFeedback,
    removeFeedback,
  };
}
