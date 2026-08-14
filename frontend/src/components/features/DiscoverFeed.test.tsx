import { configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DiscoverFeed } from "@/components/features/DiscoverFeed";
import type { FeedItem } from "@/lib/feed";
import { NavigationTestProvider, useNavigationTestContext } from "@/test-utils/next-navigation-test-context";
import { TEST_TIMEOUT_MS, WAIT_TIMEOUT_MS } from "@/test-utils/timeouts";

vi.mock("next/navigation", () => ({
  useSearchParams: () => useNavigationTestContext().searchParams,
  usePathname: () => useNavigationTestContext().pathname,
  useRouter: () => useNavigationTestContext().router,
}));

configure({ asyncUtilTimeout: WAIT_TIMEOUT_MS });

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function makeItem(overrides: Partial<FeedItem> & { article_id: string }): FeedItem {
  return {
    canonical_url: `https://example.com/${overrides.article_id}`,
    original_url: `https://example.com/${overrides.article_id}`,
    feedback: null,
    is_primary_source: false,
    is_read: false,
    language: "en",
    published_at: "2026-08-01T00:00:00Z",
    rank: 1,
    reasons: {},
    score: 0.5,
    source_domain: "example.com",
    summary_ja: null,
    technologies: [],
    title: `Title ${overrides.article_id}`,
    topics: [],
    translated_title: null,
    ...overrides,
  };
}

function makeItems(count: number, prefix: string): FeedItem[] {
  return Array.from({ length: count }, (_, index) =>
    makeItem({ article_id: `${prefix}-${String(index).padStart(4, "0")}` }),
  );
}

/** URL の `page` クエリ（省略時は 1）を読む。 */
function pageFromUrl(url: string): number {
  const page = new URL(url).searchParams.get("page");
  return page ? Number(page) : 1;
}

function renderFeed(initialSearch = ""): ReturnType<typeof render> {
  return render(
    <NavigationTestProvider initialSearch={initialSearch} pathname="/">
      <DiscoverFeed />
    </NavigationTestProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DiscoverFeed", () => {
  it("shows a loading indicator during the initial fetch", () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => new Promise<Response>(() => {})),
    );

    // Act
    renderFeed();

    // Assert
    expect(screen.getByRole("status")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("renders article cards and the total count from the first page", async () => {
    // Arrange
    const items = makeItems(3, "page1");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ items, total_count: 25, page: 1, page_size: 3, total_pages: 9 }),
      ),
    );

    // Act
    renderFeed();

    // Assert
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(3));
    expect(screen.getByText("全25件")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("requests the next page and replaces the articles when a pager button is clicked", async () => {
    // Arrange
    const page1 = makeItems(2, "page1");
    const page2 = makeItems(2, "page2");
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      const page = pageFromUrl(url);
      return jsonResponse({
        items: page === 2 ? page2 : page1,
        total_count: 4,
        page,
        page_size: 2,
        total_pages: 2,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderFeed();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "2ページ目へ" }));

    // Assert — 追記ではなく2ページ目の内容へ差し替わる
    await waitFor(() => expect(screen.getByText(page2[0]!.title)).toBeInTheDocument());
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(screen.queryByText(page1[0]!.title)).not.toBeInTheDocument();
    const lastCall = fetchMock.mock.calls.at(-1) as [string];
    expect(pageFromUrl(lastCall[0])).toBe(2);
  }, TEST_TIMEOUT_MS);

  it("shows an error message when the feed request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    renderFeed();

    // Assert
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  }, TEST_TIMEOUT_MS);

  it("shows an empty state message instead of an error when no articles match", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ items: [], total_count: 0, page: 1, page_size: 20, total_pages: 0 }),
      ),
    );

    // Act
    renderFeed();

    // Assert
    await waitFor(() =>
      expect(screen.getByText("表示できる記事がありません。")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("restores the search term from the URL into the filter form on mount", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ items: [], total_count: 0, page: 1, page_size: 20, total_pages: 0 }),
      ),
    );

    // Act
    renderFeed("q=rust");

    // Assert
    await waitFor(() => expect(screen.getByLabelText("検索語")).toHaveValue("rust"));
  }, TEST_TIMEOUT_MS);

  it("sends the search term to the API and resets to page 1 when the filter form is submitted", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      const page = pageFromUrl(url);
      return jsonResponse({
        items: page === 1 ? makeItems(2, "page1") : [],
        total_count: 2,
        page,
        page_size: 2,
        total_pages: 1,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderFeed();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.change(screen.getByLabelText("検索語"), { target: { value: "rust" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    await waitFor(() => {
      const lastCall = fetchMock.mock.calls.at(-1) as [string];
      const query = new URL(lastCall[0]).searchParams;
      expect(query.get("q")).toBe("rust");
      expect(pageFromUrl(lastCall[0])).toBe(1);
    });
  }, TEST_TIMEOUT_MS);

  it("marks the Good button as pressed optimistically when clicked", async () => {
    // Arrange
    const items = makeItems(1, "only");
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>(() => {});
      }
      return jsonResponse({ items, total_count: 1, page: 1, page_size: 20, total_pages: 1 });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderFeed();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Good" }));

    // Assert — API 応答を待たずに押下状態になる
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Good" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
  }, TEST_TIMEOUT_MS);

  it("marks the Good button as pressed even when clicked in the same tick it appears (Issue #37)", async () => {
    // Arrange — 記事が DOM に出た瞬間（React の passive effect が走る前）に
    // クリックする。以前はこの窓でクリックすると、hook が最新の items をまだ
    // 読めず、操作が黙って捨てられていた（Issue #37）。
    const items = makeItems(1, "only");
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>(() => {});
      }
      return jsonResponse({ items, total_count: 1, page: 1, page_size: 20, total_pages: 1 });
    });
    vi.stubGlobal("fetch", fetchMock);

    let clicked = false;
    const observer = new MutationObserver(() => {
      if (clicked) {
        return;
      }
      const button = screen.queryByRole("button", { name: "Good" });
      if (button === null) {
        return;
      }
      clicked = true;
      fireEvent.click(button);
    });
    observer.observe(document.body, { childList: true, subtree: true });

    try {
      // Act
      renderFeed();

      await waitFor(() => expect(clicked).toBe(true));

      // Assert
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Good" })).toHaveAttribute(
          "aria-pressed",
          "true",
        ),
      );
    } finally {
      observer.disconnect();
    }
  }, TEST_TIMEOUT_MS);
});
