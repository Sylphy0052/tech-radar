import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useInterestArticles } from "@/hooks/useInterestArticles";
import { EMPTY_ARTICLE_FILTERS } from "@/lib/interest-articles";
import type { ArticleFilters, InterestArticleItem } from "@/lib/interest-articles";

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

const itemA = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" });
const itemB = makeItem({ article_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" });
const itemC = makeItem({ article_id: "cccccccc-cccc-cccc-cccc-cccccccccccc" });

function stubListPages(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (url.includes("cursor=page-2")) {
      return jsonResponse({ items: [itemB, itemC], next_cursor: null });
    }
    return jsonResponse({ items: [itemA, itemB], next_cursor: "page-2" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("useInterestArticles", () => {
  it("loads the first page on mount", async () => {
    // Arrange
    stubListPages();

    // Act
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));

    // Assert
    expect(result.current.isLoading).toBe(true);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([itemA, itemB]);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("shows an empty state when the response has no items", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })));

    // Act
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("surfaces an error message when the request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.error).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
  });

  it("sends the given filters as query parameters", async () => {
    // Arrange
    const fetchMock = stubListPages();
    const filters: ArticleFilters = { ...EMPTY_ARTICLE_FILTERS, origin: ["good"], domain: "ai" };

    // Act
    renderHook(() => useInterestArticles(filters));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.getAll("origin")).toEqual(["good"]);
    expect(searchParams.get("domain")).toBe("ai");
  });

  it("refetches from the first page when the filters change", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [itemA], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    const { result, rerender } = renderHook(({ filters }) => useInterestArticles(filters), {
      initialProps: { filters: EMPTY_ARTICLE_FILTERS },
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act
    rerender({ filters: { ...EMPTY_ARTICLE_FILTERS, language: "ja" } });

    // Assert — フィルターが変わったら再取得する
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(callCountBefore + 1));
    const [url] = fetchMock.mock.calls[callCountBefore] as [string];
    expect(new URL(url).searchParams.get("language")).toBe("ja");
  });

  it("appends the next page without duplicating articles already present", async () => {
    // Arrange
    stubListPages();
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));

    // Assert
    expect(result.current.items.map((item) => item.article_id)).toEqual([
      itemA.article_id,
      itemB.article_id,
      itemC.article_id,
    ]);
    expect(result.current.hasMore).toBe(false);
  });

  it("does not call the API again once next_cursor is null", async () => {
    // Arrange
    const fetchMock = stubListPages();
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
    const callCountAfterExhausted = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.loadMore();
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountAfterExhausted);
  });

  it("removes the article from the list immediately when removeArticle is called", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse({ items: [itemA, itemB], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.removeArticle(itemA.article_id);
    });

    // Assert — 即座にローカル state から消え、DELETE が送信される
    expect(result.current.items.map((item) => item.article_id)).toEqual([itemB.article_id]);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/api/articles/${itemA.article_id}/interest`),
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  });

  it("rolls back and surfaces an error when the removal request fails", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response("boom", { status: 500 });
      }
      return jsonResponse({ items: [itemA, itemB], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.removeArticle(itemA.article_id);
    });

    // Assert
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.items.map((item) => item.article_id)).toEqual([
      itemA.article_id,
      itemB.article_id,
    ]);
  });

  it("ignores a second removeArticle call on the same article while the request is in flight", async () => {
    // Arrange
    let deleteCallCount = 0;
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        deleteCallCount += 1;
        return new Response(null, { status: 204 });
      }
      return jsonResponse({ items: [itemA], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.removeArticle(itemA.article_id);
      result.current.removeArticle(itemA.article_id);
    });

    // Assert
    await waitFor(() => expect(deleteCallCount).toBe(1));
  });

  it("does nothing when removeArticle targets an unknown article id", async () => {
    // Arrange
    const fetchMock = stubListPages();
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.removeArticle("unknown-id");
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
  });
});
