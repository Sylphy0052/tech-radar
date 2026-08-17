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
  totalPages: number;
  totalCount: number;
  /** 記事を関心記事一覧から除外する（`DELETE /api/articles/{article_id}/interest`）。 */
  removeArticle: (articleId: string) => void;
}

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
 * ページ番号はこの hook では持たず、呼び出し側（`InterestArticleList`）が URL から
 * 読んで引数で渡す（Issue #100、フィード側は Issue #95 で対応済みの `useFeed` と
 * 同じ設計）。URL を唯一の情報源にすることで、リロード・共有・戻る操作でページが
 * 再現する。フィルター条件が URL クエリだけを情報源にしているのと同じ扱いで、
 * 状態の持ち方が非対称でなくなる。
 *
 * そのため「フィルターが変わったらページを1へ戻す」処理はここには無い。`page` を
 * `buildSearchParamsFromFilters` の対象に含めていないため、`ArticleFilterPanel` が
 * フィルター変更時に URL を組み立て直すと `page` は自然に落ちる。**将来
 * `buildSearchParamsFromFilters` へ `page` を足すとこの性質が壊れる**（フィルターを
 * 変えてもページ番号が残り、範囲外のページを見せてしまう。`useFeed` と同じ注意）。
 *
 * フィルターとページのどちらが変わったかは「レンダー中に前回値と比較して直す」
 * React の公式パターンで検出し、読み込み中の表示へ切り替える
 * （https://react.dev/learn/you-might-not-need-an-effect の
 * "Adjusting state when a prop changes"）。`useEffect` の本体で setState を
 * 同期的に呼ぶとカスケード再レンダーになるため（react-hooks/set-state-in-effect
 * が検出する）、この検出はレンダー中のこちらへ寄せ、`useEffect` 側は非同期の
 * fetch とその結果を反映する setState だけにする。フィルターの比較はオブジェクト
 * 参照ではなくクエリ文字列で行うので、呼び出し側が毎レンダー新しい `filters`
 * オブジェクトを渡しても（値が同じなら）無駄なリセットは起きない。
 *
 * 除外操作は `useFeed` の `removeFeedback` と同じく、先にローカル state から
 * 消してから API を呼び、失敗時はもとの位置へ戻す。
 */
export function useInterestArticles(filters: ArticleFilters, page: number): UseInterestArticlesResult {
  const [items, setItems] = useState<InterestArticleItem[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [pageSize, setPageSize] = useState(0);
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

  // 総ページ数は state で持たず総件数から導く。除外操作は総件数をローカルで
  // 1件ぶん減らすため（下記 `removeArticle`）、応答の `total_pages` をそのまま
  // 保持すると再取得までページ数だけが古い値で残る。backend の計算式と同じ
  // （`ceil(total_count / page_size)`）なので、導出しても値は変わらない。
  const totalPages = pageSize > 0 ? Math.ceil(totalCount / pageSize) : 0;

  const filterKey = buildSearchParamsFromFilters(filters).toString();
  const [previousFilterKey, setPreviousFilterKey] = useState(filterKey);
  if (filterKey !== previousFilterKey) {
    setPreviousFilterKey(filterKey);
    setItems([]);
    setError(null);
    setIsLoading(true);
  }

  // ページ移動でも読み込み中にする。`useEffect` の本体で `setIsLoading(true)` を
  // 同期的に呼ぶとカスケード再レンダーになるため、読み込み開始の合図はフィルター
  // 変更と同じくレンダー中の比較で立て、`effect` 側は結果反映の setState だけに
  // する（`useFeed` と同じ形）。
  //
  // 前の items を残さないのは、範囲外のページを開いたときに前ページの記事が
  // 見えたままにならないようにするため（`InterestArticleList` は items が
  // 空のときに「このページには記事がありません」を出す）。
  const [previousPage, setPreviousPage] = useState(page);
  if (page !== previousPage) {
    setPreviousPage(page);
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
        setTotalCount(response.total_count);
        setPageSize(response.page_size);
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

      // 総件数も一緒に減らす。`items` だけ減らすと「全件が空なのか、その
      // ページだけ空なのか」の出し分け（`InterestArticleList`）と「全N件」の
      // 表示が、次の取得まで古い総件数のまま食い違う。
      setItems((current) => withoutArticle(current, articleId));
      setTotalCount((current) => Math.max(0, current - 1));
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
          setTotalCount((current) => current + 1);
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
    totalPages,
    totalCount,
    removeArticle,
  };
}
