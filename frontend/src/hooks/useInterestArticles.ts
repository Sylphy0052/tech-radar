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
  error: string | null;
  /** 1始まりの現在ページ番号。 */
  page: number;
  totalPages: number;
  totalCount: number;
  /** ページを移動する。`Pagination` の `onPageChange` へそのまま渡す想定。 */
  setPage: (page: number) => void;
  /** 記事を関心記事一覧から除外する（`DELETE /api/articles/{article_id}/interest`）。 */
  removeArticle: (articleId: string) => void;
}

const FIRST_PAGE = 1;

/** 指定 article_id を除いた新しい配列を返す。 */
function withoutArticle(items: InterestArticleItem[], articleId: string): InterestArticleItem[] {
  return items.filter((item) => item.article_id !== articleId);
}

/**
 * 関心記事一覧の状態管理 hook（`useFeed` と同じ構成）。
 *
 * Issue #91 で「さらに読み込む」（cursor）から番号付きページングへ書き換えた。
 * ページは追記せず丸ごと差し替える（backend の `page` は offset で、cursor と違い
 * 同じページを何度でも取り直せるため、蓄積して重複排除する必要が無くなった）。
 *
 * フィルター条件が変わったかどうかは「レンダー中に前回値と比較して直す」React の
 * 公式パターンで検出し、ページ番号を1へ戻す
 * （https://react.dev/learn/you-might-not-need-an-effect の
 * "Adjusting state when a prop changes"）。`useEffect` の本体で setState を
 * 同期的に呼ぶとカスケード再レンダーになるため（react-hooks/set-state-in-effect
 * が検出する）、リセットはレンダー中のこちらへ寄せ、`useEffect` 側は非同期の
 * fetch とその結果を反映する setState だけにする。オブジェクト参照ではなく
 * クエリ文字列で比較するので、呼び出し側が毎レンダー新しい `filters`
 * オブジェクトを渡しても（値が同じなら）無駄なリセットは起きない。
 *
 * 除外操作は `useFeed` の `removeFeedback` と同じく、先にローカル state から
 * 消してから API を呼び、失敗時はもとの位置へ戻す。
 */
export function useInterestArticles(filters: ArticleFilters): UseInterestArticlesResult {
  const [items, setItems] = useState<InterestArticleItem[]>([]);
  const [page, setPageState] = useState(FIRST_PAGE);
  const [totalPages, setTotalPages] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

    listInterestArticles(filters, { page })
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
    // `filters` を直接依存に含める。呼び出し側（`InterestArticleList`）が `useMemo` で
    // 参照を安定させる想定だが、安定していなくても（`filterKey` の値が同じなら
    // 上のリセットが起きないため）正しく動く。フィルターとページのどちらの変更も
    // この effect が担い、`cancelled` フラグで古いレスポンスの反映を防ぐ
    // （`useFeed` と同じ形。書き換え前の `filterKeyRef` による世代判定は、
    // 追記をやめてページ差し替えにしたため不要になった）。
  }, [filters, page]);

  // ページ移動でも読み込み中にする。`useEffect` の本体で `setIsLoading(true)` を
  // 同期的に呼ぶとカスケード再レンダーになるため、読み込み開始の合図は「取得の
  // きっかけを作る側」であるここと上のフィルター変更検出へ寄せる（`useFeed` と
  // 同じ形）。同じページ番号への移動では effect が走らないので、読み込み中のまま
  // 止まらないよう先に弾く。
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
    error,
    page,
    totalPages,
    totalCount,
    setPage,
    removeArticle,
  };
}
