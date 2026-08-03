"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { isRateLimitError } from "@/lib/api";
import { getFeed } from "@/lib/feed";
import type { FeedItem } from "@/lib/feed";
import { deleteFeedback, sendFeedback } from "@/lib/feedback";
import type { ArticleFeedback, BadReason, FeedbackAction } from "@/lib/feedback";
import { getRateLimitMessage, getRequestErrorMessage } from "@/lib/request-error-message";

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

/**
 * `Retry-After` が読めなかったときのクールダウン。backend のレート制限
 * ウィンドウ既定値（`recommendation_rate_limit_window_seconds` = 60 秒）に合わせる。
 */
const DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60;

/**
 * 429 を受けたときだけ、追加ロードを止める期限（epoch ミリ秒）を更新する。
 *
 * 無限スクロールはセンチネルが可視になるたびに `loadMore` を呼ぶため、
 * 制限に掛かった直後にそのまま再試行すると残りの許容回数を食い潰し、
 * 待機時間だけが伸びていく。
 */
function applyRateLimitCooldown(untilRef: { current: number }, error: unknown): void {
  if (!isRateLimitError(error)) {
    return;
  }
  const waitSeconds = error.retryAfterSeconds ?? DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS;
  untilRef.current = Date.now() + waitSeconds * 1000;
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
 * 最新の items はレンダー時の `items` を直接参照する（以前は `itemsRef` という
 * `useEffect` 経由のミラーを使っていたが、DOM へのコミットと passive effect の
 * 実行の間には窓があり、その間にクリックされると古い（空の場合もある）配列を
 * 読んでフィードバックが黙って捨てられていた。Issue #37）。`setState` の関数形式
 * アップデータ内で API 呼び出しのような副作用を行うと、React が purity チェックの
 * ためアップデータを二重に呼ぶ場合に副作用も二重発火してしまうため、読み取りと
 * 更新を分離している（`usePolling` の `fetchFnRef` と同じ狙い）。
 */
export function useFeed(): UseFeedResult {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // loadMore の参照を安定させるための最新値。state と二重管理になるが、
  // setState と同じタイミングで同期的に更新することで、
  // 「setState 直後の再呼び出しでは古い値が見える」問題（レンダー確定・
  // passive effect 実行を待って初めて更新される値だけに頼ると起きうる）を避ける。
  const nextCursorRef = useRef<string | null>(null);
  const isLoadingMoreRef = useRef(false);

  // レート制限（429）を受けたあと、追加ロードを再開してよい時刻（epoch ミリ秒）。
  const rateLimitedUntilRef = useRef(0);

  // 送信中（pending）の article_id 集合。同じボタンを再レンダリングを挟まず
  // 連打すると、1回目の楽観的更新がまだレンダーに反映されていない古い feedback を
  // 読んでしまい、既に取り消し済みの feedback へ再度 DELETE を送ってロールバックで
  // 復活させてしまう（サーバー側 404 → catch）。読み取りタイミングを詰めるよりも
  // 「送信中は同じ記事への操作を無視する」方が意図が明確で、他の更新経路が
  // 増えても壊れにくいためこちらを選ぶ。
  const pendingArticleIdsRef = useRef<Set<string>>(new Set());

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
        nextCursorRef.current = response.next_cursor;
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) {
          return;
        }
        // 初回ロードの失敗ではクールダウンを張らない。next_cursor が未取得のため
        // loadMore はもともと何もせず、抑止する対象が存在しない。
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

  // 依存配列を空にして参照を安定させる（nextCursor / isLoadingMore の値は
  // ref から読む）。これらの state を依存に含めると、フェッチの開始・終了の
  // たびに loadMore の参照が変わり、これを依存配列に持つ呼び出し側の
  // useEffect（IntersectionObserver のセットアップ）がページを読むたびに
  // observer を disconnect して作り直してしまう。
  const loadMore = useCallback(() => {
    if (nextCursorRef.current === null || isLoadingMoreRef.current) {
      return;
    }
    const remainingMs = rateLimitedUntilRef.current - Date.now();
    if (remainingMs > 0) {
      // レート制限中は追加ロードを行わない。他の操作の成功でエラー表示が消えていても
      // 「押しても何も起きない」状態にならないよう、残り時間を計算し直して出す。
      setError(getRateLimitMessage(Math.ceil(remainingMs / 1000)));
      return;
    }
    const cursor = nextCursorRef.current;
    isLoadingMoreRef.current = true;
    setIsLoadingMore(true);
    getFeed({ cursor })
      .then((response) => {
        if (!isMountedRef.current) {
          return;
        }
        setItems((current) => mergeItems(current, response.items));
        setNextCursor(response.next_cursor);
        nextCursorRef.current = response.next_cursor;
        setError(null);
      })
      .catch((err: unknown) => {
        if (!isMountedRef.current) {
          return;
        }
        applyRateLimitCooldown(rateLimitedUntilRef, err);
        setError(getRequestErrorMessage(err));
      })
      .finally(() => {
        isLoadingMoreRef.current = false;
        if (!isMountedRef.current) {
          return;
        }
        setIsLoadingMore(false);
      });
  }, []);

  const applyFeedback = useCallback(
    (articleId: string, action: FeedbackAction, reason?: BadReason) => {
      if (pendingArticleIdsRef.current.has(articleId)) {
        // 送信中の同じ記事への再クリックは無視する（G-1 参照）。
        return;
      }

      const target = items.find((item) => item.article_id === articleId);
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
      pendingArticleIdsRef.current.add(articleId);

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
        })
        .finally(() => {
          pendingArticleIdsRef.current.delete(articleId);
        });
    },
    [items],
  );

  const removeFeedback = useCallback(
    (articleId: string) => {
      if (pendingArticleIdsRef.current.has(articleId)) {
        // 送信中の同じ記事への再クリックは無視する（applyFeedback と同じ理由、G-1 参照）。
        return;
      }

      const target = items.find((item) => item.article_id === articleId);
      if (!target || target.feedback === null) {
        return;
      }
      const previousFeedback = target.feedback;

      setItems((current) => withFeedback(current, articleId, null));
      pendingArticleIdsRef.current.add(articleId);

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
    applyFeedback,
    removeFeedback,
  };
}
