import { configure, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ArticlesPage from "@/app/articles/page";
import type { InterestArticleItem } from "@/lib/interest-articles";
import { NavigationTestProvider, useNavigationTestContext } from "@/test-utils/next-navigation-test-context";
import { TEST_TIMEOUT_MS, WAIT_TIMEOUT_MS } from "@/test-utils/timeouts";

vi.mock("next/navigation", () => ({
  useSearchParams: () => useNavigationTestContext().searchParams,
  usePathname: () => useNavigationTestContext().pathname,
  useRouter: () => useNavigationTestContext().router,
}));

configure({ asyncUtilTimeout: WAIT_TIMEOUT_MS });

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function makeItem(overrides: Partial<InterestArticleItem> & { article_id: string }): InterestArticleItem {
  return {
    canonical_url: `https://example.com/${overrides.article_id}`,
    original_url: `https://example.com/${overrides.article_id}`,
    category: null,
    content_type: null,
    domain: null,
    is_primary_source: true,
    language: "ja",
    origin: "manual",
    published_at: "2026-07-28T00:00:00Z",
    registered_at: "2026-08-01T12:00:00Z",
    source_domain: "example.com",
    title: "Title",
    topics: [],
    translated_title: null,
    ...overrides,
  };
}

describe("ArticlesPage", () => {
  it("renders the heading", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })));

    // Act
    render(
      <NavigationTestProvider>
        <ArticlesPage />
      </NavigationTestProvider>,
    );

    // Assert
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("関心記事一覧");
  }, TEST_TIMEOUT_MS);

  it("fetches with the filters restored from the URL and shows them in the filter form", async () => {
    // Arrange — URL に既にフィルターが乗った状態でマウントする（リロード相当）
    const item = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title: "絞り込み結果" });
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [item], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    render(
      <NavigationTestProvider initialSearch="origin=good&language=ja">
        <ArticlesPage />
      </NavigationTestProvider>,
    );

    // Assert — フィルター UI に復元され、その条件で API が呼ばれている
    await waitFor(() => expect(screen.getByText("絞り込み結果")).toBeInTheDocument());
    expect(screen.getByRole("checkbox", { name: "Good" })).toBeChecked();
    expect(screen.getByLabelText("言語")).toHaveValue("ja");

    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.getAll("origin")).toEqual(["good"]);
    expect(searchParams.get("language")).toBe("ja");
  }, TEST_TIMEOUT_MS);
});
