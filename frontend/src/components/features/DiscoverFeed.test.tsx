import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DiscoverFeed } from "@/components/features/DiscoverFeed";
import type { FeedItem } from "@/lib/feed";

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

type IntersectionEntryStub = Pick<IntersectionObserverEntry, "isIntersecting">;

/** jsdom には IntersectionObserver が無いため、テスト側で最小限のモックを用意する。 */
class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = [];
  callback: (entries: IntersectionEntryStub[]) => void;
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  takeRecords = vi.fn(() => []);

  constructor(callback: (entries: IntersectionEntryStub[]) => void) {
    this.callback = callback;
    MockIntersectionObserver.instances.push(this);
  }
}

function triggerIntersection(isIntersecting: boolean): void {
  const instance = MockIntersectionObserver.instances.at(-1);
  instance?.callback([{ isIntersecting }]);
}

beforeEach(() => {
  MockIntersectionObserver.instances = [];
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DiscoverFeed", () => {
  it("shows a loading indicator during the initial fetch", () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => new Promise<Response>(() => {})),
    );

    // Act
    render(<DiscoverFeed />);

    // Assert
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders 20 article cards from the first page", async () => {
    // Arrange
    const items = makeItems(20, "page1");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items, next_cursor: null })),
    );

    // Act
    render(<DiscoverFeed />);

    // Assert
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(20));
  });

  it("appends the next page without duplicating articles when the sentinel becomes visible", async () => {
    // Arrange
    const page1 = makeItems(2, "page1");
    const page2 = makeItems(2, "page2");
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("cursor=page-2")) {
        return jsonResponse({ items: page2, next_cursor: null });
      }
      return jsonResponse({ items: page1, next_cursor: "page-2" });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DiscoverFeed />);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act — センチネルが可視になったことを通知する
    act(() => {
      triggerIntersection(true);
    });

    // Assert — 4 件（重複なし）に増える
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(4));
    const renderedIds = [...page1, ...page2].map((item) => item.article_id);
    expect(new Set(renderedIds).size).toBe(4);
  });

  it("does not request another page and shows the end message once hasMore is false", async () => {
    // Arrange
    const items = makeItems(1, "only");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items, next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DiscoverFeed />);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act — hasMore が false のときにセンチネルが可視になっても何もしない
    act(() => {
      triggerIntersection(true);
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
    expect(screen.getByText("すべての記事を読み込みました")).toBeInTheDocument();
  });

  it("shows an error message when the feed request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    render(<DiscoverFeed />);

    // Assert
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("shows an empty state message when there are no articles", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })),
    );

    // Act
    render(<DiscoverFeed />);

    // Assert
    await waitFor(() =>
      expect(screen.getByText("表示できる記事がありません。")).toBeInTheDocument(),
    );
  });

  it("marks the Good button as pressed optimistically when clicked", async () => {
    // Arrange
    const items = makeItems(1, "only");
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>(() => {});
      }
      return jsonResponse({ items, next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DiscoverFeed />);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Good" }));

    // Assert — API 応答を待たずに押下状態になる
    expect(screen.getByRole("button", { name: "Good" })).toHaveAttribute("aria-pressed", "true");
  });
});
