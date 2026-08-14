"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { buildSearchParamsFromFilters, getFeed } from "@/lib/feed";
import type { FeedFilters, FeedItem } from "@/lib/feed";
import { deleteFeedback, sendFeedback } from "@/lib/feedback";
import type { ArticleFeedback, BadReason, FeedbackAction } from "@/lib/feedback";
import { reportUnexpectedState } from "@/lib/report-unexpected";
import { getRequestErrorMessage } from "@/lib/request-error-message";

interface UseFeedResult {
  items: FeedItem[];
  isLoading: boolean;
  error: string | null;
  /** 1始まりの現在ページ番号。 */
  page: number;
  totalPages: number;
  totalCount: number;
  /** ページを移動する。`Pagination` の `onPageChange` へそのまま渡す想定。 */
  setPage: (page: number) => void;
  applyFeedback: (articleId: string, action: FeedbackAction, reason?: BadReason) => void;
  removeFeedback: (articleId: string) => void;
}

const FIRST_PAGE = 1;

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
 * Issue #90 で無限スクロール（cursor）から番号付きページングへ書き換えた。
 * ページは追記（append）せず丸ごと差し替える（backend の `page` は run 内の
 * rank に対する offset で、cursor と違い同じページを何度でも取り直せる設計の
 * ため、蓄積して重複排除する必要が無くなった）。
 *
 * フィルターが変わったかどうかは `useInterestArticles` と同じ「レンダー中に
 * 前回値と比較して直す」パターンで検出し、ページ番号を1へ戻す
 * （https://react.dev/learn/you-might-not-need-an-effect の
 * "Adjusting state when a prop changes"）。フィルター・ページのどちらの変更も
 * 同じ effect が取得を担い、`cancelled` フラグで古いレスポンスの反映を防ぐ
 * （`filters` か `page` が変わるたびに cleanup が走り、直前のフェッチの結果を
 * 無効化する。`useInterestArticles.loadMore` の `filterKeyRef` と同じ「古い
 * レスポンスを破棄する」狙いを、こちらは effect の cleanup で満たす）。
 *
 * フィードバックの楽観的更新（Good/Bad/保存）は書き換え前の `useFeed` と同じ
 * 設計を引き継ぐ：先にローカル state を書き換えてから API を呼び、失敗時は
 * 呼び出し前の状態へロールバックする。最新の items はレンダー時の `items` を
 * 直接参照する（Issue #37 の教訓、`itemsRef` 経由のミラーは使わない）。
 */
export function useFeed(filters: FeedFilters): UseFeedResult {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [page, setPageState] = useState(FIRST_PAGE);
  const [totalPages, setTotalPages] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  // フィルターが変わったかどうかを、クエリ文字列で比較して検出する。オブジェクト
  // 参照ではなく文字列で比較するので、呼び出し側が毎レンダー新しい `filters`
  // オブジェクトを渡しても（値が同じなら）無駄なリセットは起きない
  // （`useInterestArticles` と同じ狙い）。
  const filterKey = buildSearchParamsFromFilters(filters).toString();
  const [previousFilterKey, setPreviousFilterKey] = useState(filterKey);
  if (filterKey !== previousFilterKey) {
    setPreviousFilterKey(filterKey);
    setPageState(FIRST_PAGE);
    setItems([]);
    setError(null);
    setIsLoading(true);
  }

  useEffect(() => {
    let cancelled = false;
    getFeed(filters, { page })
      .then((response) => {
        if (cancelled) {
          return;
        }
        setItems(response.items);
        setTotalPages(response.total_pages);
        setTotalCount(response.total_count);
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
    // `filters` を直接依存に含める。呼び出し側（`DiscoverFeed`）が `useMemo` で
    // 参照を安定させる想定だが、安定していなくても（`filterKey` の値が同じなら
    // 上のリセットが起きないため）正しく動く（`useInterestArticles` と同じ狙い）。
  }, [filters, page]);

  // ページ移動でも読み込み中にする。`useEffect` の本体で `setIsLoading(true)` を
  // 同期的に呼ぶとカスケード再レンダーになる（react-hooks/set-state-in-effect が
  // 検出する）ため、読み込み開始の合図は「取得のきっかけを作る側」であるここと
  // 上のフィルター変更検出へ寄せ、effect 側は結果反映の setState だけにする
  // （`useInterestArticles` と同じ形）。同じページ番号への移動では effect が
  // 走らないので、読み込み中のまま止まらないよう先に弾く。
  const setPage = useCallback(
    (nextPage: number) => {
      if (nextPage === page) {
        return;
      }
      setPageState(nextPage);
      setIsLoading(true);
    },
    [page],
  );

  const applyFeedback = useCallback(
    (articleId: string, action: FeedbackAction, reason?: BadReason) => {
      if (pendingArticleIdsRef.current.has(articleId)) {
        // 送信中の同じ記事への再クリックは無視する（G-1 参照）。
        return;
      }

      const target = items.find((item) => item.article_id === articleId);
      if (!target) {
        // 画面に出ている記事のクリックなら、そのレンダーの items に必ず含まれる
        // （itemsRef 経由の遅れた読み取りは Issue #37 で撤去済み）。ここへ来るのは
        // 一覧に無い article_id を渡されたときだけで、その場合の握り潰しは
        // 「押しても何も起きない」に見える（Issue #45）。本番の表示は変えずに
        // 開発時だけ痕跡を残す。
        reportUnexpectedState(`applyFeedback: 一覧に無いarticle_id: ${articleId}`);
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
      if (!target) {
        // applyFeedback と同じ理由で警告する（Issue #45）。
        reportUnexpectedState(`removeFeedback: 一覧に無いarticle_id: ${articleId}`);
        return;
      }
      if (target.feedback === null) {
        // 取り消すものが無いだけで、これは正常な呼び出し（警告しない）。
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
    error,
    page,
    totalPages,
    totalCount,
    setPage,
    applyFeedback,
    removeFeedback,
  };
}
