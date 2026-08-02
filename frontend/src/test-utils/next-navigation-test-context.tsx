"use client";

import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

interface NavigationTestRouter {
  replace: (href: string) => void;
  push: (href: string) => void;
}

interface NavigationTestContextValue {
  searchParams: URLSearchParams;
  pathname: string;
  router: NavigationTestRouter;
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

  const value = useMemo<NavigationTestContextValue>(() => {
    function navigate(href: string): void {
      const queryIndex = href.indexOf("?");
      setSearch(queryIndex === -1 ? "" : href.slice(queryIndex + 1));
    }
    return {
      searchParams: new URLSearchParams(search),
      pathname,
      router: { replace: navigate, push: navigate },
    };
  }, [search, pathname]);

  return <NavigationTestContext.Provider value={value}>{children}</NavigationTestContext.Provider>;
}

export function useNavigationTestContext(): NavigationTestContextValue {
  const context = useContext(NavigationTestContext);
  if (context === null) {
    throw new Error("NavigationTestProvider の外で next/navigation のモックが呼ばれました");
  }
  return context;
}
