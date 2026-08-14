"use client";

import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

interface NavigationTestRouter {
  replace: (href: string) => void;
  push: (href: string) => void;
}

/** 1回ぶんのナビゲーション。`push` と `replace` の使い分けを検証するために記録する。 */
export interface NavigationRecord {
  href: string;
  kind: "push" | "replace";
}

interface NavigationTestContextValue {
  searchParams: URLSearchParams;
  pathname: string;
  router: NavigationTestRouter;
  /**
   * これまでに起きたナビゲーションの記録（古い順）。
   *
   * `push` と `replace` はどちらもこの Provider では同じ URL 更新になるため、
   * 記録が無いとテストから区別できない。ブラウザの「戻る」で1つ前のページへ帰れる
   * かどうかは `push` を使っているかで決まる（Issue #95 のフィードのページ番号）。
   */
  navigations: NavigationRecord[];
}

const NavigationTestContext = createContext<NavigationTestContextValue | null>(null);

interface NavigationTestProviderProps {
  /** 初期 URL のクエリ文字列（先頭の `?` は付けない）。 */
  initialSearch?: string;
  pathname?: string;
  children: ReactNode;
}

/**
 * `next/navigation` の `useSearchParams` / `usePathname` / `useRouter` をテストで
 * 差し替えるための Provider。
 *
 * `router.replace` / `router.push` で更新した URL が `useSearchParams` の
 * 戻り値へ反映されるよう、実際の React state で URL を保持する（モジュール
 * スコープの可変変数だけだと再レンダリングが起きず、ArticleFilterPanel が
 * 更新した URL を InterestArticleList 側が拾えないテストになってしまうため）。
 */
export function NavigationTestProvider({
  initialSearch = "",
  pathname = "/articles",
  children,
}: NavigationTestProviderProps) {
  const [search, setSearch] = useState(initialSearch);
  // 記録は ref ではなく state で持つ。ref だとレンダー中に `current` を読むことになり、
  // `react-hooks/refs` が「レンダー中に ref へ触るな」と止める。`setSearch` と同じ
  // イベントの中で呼ぶのでバッチされ、再レンダリングは1回で済む。
  const [navigations, setNavigations] = useState<NavigationRecord[]>([]);

  const value = useMemo<NavigationTestContextValue>(() => {
    function navigate(href: string, kind: NavigationRecord["kind"]): void {
      setNavigations((current) => [...current, { href, kind }]);
      const queryIndex = href.indexOf("?");
      setSearch(queryIndex === -1 ? "" : href.slice(queryIndex + 1));
    }
    return {
      searchParams: new URLSearchParams(search),
      pathname,
      router: {
        replace: (href: string) => navigate(href, "replace"),
        push: (href: string) => navigate(href, "push"),
      },
      navigations,
    };
  }, [search, pathname, navigations]);

  return <NavigationTestContext.Provider value={value}>{children}</NavigationTestContext.Provider>;
}

export function useNavigationTestContext(): NavigationTestContextValue {
  const context = useContext(NavigationTestContext);
  if (context === null) {
    throw new Error("NavigationTestProvider の外で next/navigation のモックが呼ばれました");
  }
  return context;
}
