import { act, configure, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useInterestArticles } from "@/hooks/useInterestArticles";
import { EMPTY_ARTICLE_FILTERS } from "@/lib/interest-articles";
import type { ArticleFilters, InterestArticleItem } from "@/lib/interest-articles";
import { TEST_TIMEOUT_MS, WAIT_TIMEOUT_MS } from "@/test-utils/timeouts";

configure({ asyncUtilTimeout: WAIT_TIMEOUT_MS });

afterEach(() => {
  vi.unstubAllGlobals();
  // console.warn の spy（未知の article_id を渡したときの警告の検証で使う）を戻す。
  vi.restoreAllMocks();
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
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

  it("surfaces an error message when the request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.error).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

  it("discards a stale loadMore response that resolves after the filters changed", async () => {
    // Arrange — フィルターA の loadMore を発行した直後（レスポンス到達前）にフィルターを B へ切り替える
    const filtersA: ArticleFilters = { ...EMPTY_ARTICLE_FILTERS, domain: "a" };
    const filtersB: ArticleFilters = { ...EMPTY_ARTICLE_FILTERS, domain: "b" };
    const itemA1 = makeItem({ article_id: "10000000-0000-0000-0000-000000000001" });
    const itemA2 = makeItem({ article_id: "10000000-0000-0000-0000-000000000002" });
    const itemB1 = makeItem({ article_id: "20000000-0000-0000-0000-000000000001" });

    let resolveStaleLoadMore!: (response: Response) => void;
    const staleLoadMorePromise = new Promise<Response>((resolve) => {
      resolveStaleLoadMore = resolve;
    });

    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      const searchParams = new URL(url).searchParams;
      const domain = searchParams.get("domain");
      const cursor = searchParams.get("cursor");
      if (domain === "a" && cursor === "page-a-2") {
        // フィルターA の loadMore 分だけ、あえて解決を保留する
        return staleLoadMorePromise;
      }
      if (domain === "a") {
        return jsonResponse({ items: [itemA1], next_cursor: "page-a-2" });
      }
      return jsonResponse({ items: [itemB1], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result, rerender } = renderHook(({ filters }) => useInterestArticles(filters), {
      initialProps: { filters: filtersA },
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([itemA1]);

    // Act
    act(() => {
      result.current.loadMore();
    });
    rerender({ filters: filtersB });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([itemB1]);

    // フィルターA 向けの loadMore レスポンスが、フィルター切替の後から解決する
    await act(async () => {
      resolveStaleLoadMore(jsonResponse({ items: [itemA2], next_cursor: null }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // Assert — 旧フィルターの記事が新フィルターの結果へ混ざらない
    expect(result.current.items.map((item) => item.article_id)).toEqual([itemB1.article_id]);
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

  it("does nothing when removeArticle targets an unknown article id", async () => {
    // Arrange
    const fetchMock = stubListPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.removeArticle("unknown-id");
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
    // 黙って戻らず、開発時には原因を追える痕跡が残る（Issue #45）。
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0]?.[0]).toContain("unknown-id");
  }, TEST_TIMEOUT_MS);

  it("hands out a removal callback that sees the items already rendered (G-3)", async () => {
    // Arrange — 一覧が出た直後の除外操作を取りこぼさないことを固定する
    // （`useFeed` の同名テストと同じ狙い。Issue #45）。
    stubListPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    const removeArticleWhileEmpty = result.current.removeArticle;
    expect(result.current.items).toEqual([]);

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const removeArticleAfterLoad = result.current.removeArticle;

    // Assert — items が入れ替わったらハンドラも作り直される
    expect(removeArticleAfterLoad).not.toBe(removeArticleWhileEmpty);

    // Act — ロード後のハンドラは対象を見つけて即座に一覧から外す
    act(() => {
      removeArticleAfterLoad(itemA.article_id);
    });

    // Assert
    expect(result.current.items.map((item) => item.article_id)).not.toContain(itemA.article_id);
    expect(warn).not.toHaveBeenCalled();
  }, TEST_TIMEOUT_MS);

  it("warns instead of silently dropping the removal when a stale callback is used (G-3)", async () => {
    // Arrange — items が空だった頃のハンドラを、ロード後に呼ぶ。
    // 以前 `useInterestArticles` が踏んでいた窓（DOM には記事が出ているのに
    // ハンドラ側の items がまだ空）の再現であり、握り潰さずに痕跡が残ることを
    // 固定する（`useFeed` の同名テストと対になる。Issue #45）。
    stubListPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    const removeArticleWhileEmpty = result.current.removeArticle;
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      removeArticleWhileEmpty(itemA.article_id);
    });

    // Assert — 一覧からは外れず、代わりに警告が出る
    expect(result.current.items.map((item) => item.article_id)).toContain(itemA.article_id);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0]?.[0]).toContain(itemA.article_id);
  }, TEST_TIMEOUT_MS);
});
