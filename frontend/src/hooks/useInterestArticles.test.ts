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

/** URL の `page` クエリ（省略時は 1）を読む（`useFeed.test.ts` と同じ）。 */
function pageFromUrl(url: string): number {
  const page = new URL(url).searchParams.get("page");
  return page ? Number(page) : 1;
}

/**
 * ページ番号ごとに固定の items を返す fetch のスタブ。応答に含めない
 * `total_count` / `total_pages` は呼び出し側が固定値として渡す。
 */
function stubListPages(
  pages: Record<number, InterestArticleItem[]> = { 1: [itemA, itemB], 2: [itemB, itemC] },
  { totalPages = 2, totalCount = 4 }: { totalPages?: number; totalCount?: number } = {},
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === "DELETE") {
      return new Response(null, { status: 204 });
    }
    const page = pageFromUrl(url);
    return jsonResponse({
      items: pages[page] ?? [],
      total_count: totalCount,
      page,
      page_size: 2,
      total_pages: totalPages,
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** 1ページに収まる件数の応答（除外操作の検証で使う）。 */
function listResponse(items: InterestArticleItem[]): unknown {
  return {
    items,
    total_count: items.length,
    page: 1,
    page_size: 20,
    total_pages: items.length > 0 ? 1 : 0,
  };
}

describe("useInterestArticles", () => {
  it("loads page 1 with the given filters on mount", async () => {
    // Arrange
    const fetchMock = stubListPages();

    // Act
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));

    // Assert
    expect(result.current.isLoading).toBe(true);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([itemA, itemB]);
    expect(result.current.page).toBe(1);
    expect(result.current.totalPages).toBe(2);
    expect(result.current.totalCount).toBe(4);
    expect(result.current.error).toBeNull();
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(pageFromUrl(url)).toBe(1);
  }, TEST_TIMEOUT_MS);

  it("replaces items with the requested page instead of appending", async () => {
    // Arrange
    stubListPages();
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.setPage(2);
    });
    await waitFor(() => expect(result.current.page).toBe(2));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Assert — 2ページ目の内容にまるごと差し替わる（追記ではない）
    expect(result.current.items).toEqual([itemB, itemC]);
  }, TEST_TIMEOUT_MS);

  it("requests the API again with the new page number when setPage is called", async () => {
    // Arrange
    const fetchMock = stubListPages({ 1: [itemA], 2: [itemB] });
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.setPage(2);
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Assert
    const lastCall = fetchMock.mock.calls.at(-1) as [string];
    expect(pageFromUrl(lastCall[0])).toBe(2);
  }, TEST_TIMEOUT_MS);

  it("discards a stale response when the page changes again before the first request resolves", async () => {
    // Arrange — ページ2の応答をわざと遅らせ、その間にページ3へ移動する
    let resolvePageTwo!: (response: Response) => void;
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      const page = pageFromUrl(url);
      if (page === 2) {
        return new Promise<Response>((resolve) => {
          resolvePageTwo = resolve;
        });
      }
      return jsonResponse({
        items: page === 3 ? [itemC] : [itemA],
        total_count: 6,
        page,
        page_size: 2,
        total_pages: 3,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.setPage(2);
    });
    act(() => {
      result.current.setPage(3);
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // ページ2の応答が（3へ移った）後から解決しても反映されない
    resolvePageTwo(
      jsonResponse({ items: [itemB], total_count: 6, page: 2, page_size: 2, total_pages: 3 }),
    );

    // Assert
    expect(result.current.page).toBe(3);
    expect(result.current.items).toEqual([itemC]);
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when the response has no items", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([]))));

    // Act
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([]);
    expect(result.current.totalCount).toBe(0);
    expect(result.current.totalPages).toBe(0);
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

  it("resets to page 1 and refetches when the filters change", async () => {
    // Arrange
    const fetchMock = stubListPages({ 1: [itemA], 2: [itemB] });
    const { result, rerender } = renderHook(({ filters }) => useInterestArticles(filters), {
      initialProps: { filters: EMPTY_ARTICLE_FILTERS },
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    act(() => {
      result.current.setPage(2);
    });
    await waitFor(() => expect(result.current.page).toBe(2));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act — 条件を変える
    rerender({ filters: { ...EMPTY_ARTICLE_FILTERS, language: "ja" } });

    // Assert — 受入基準: 条件を変えたらページ番号が1へ戻る
    await waitFor(() => expect(result.current.page).toBe(1));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const lastCall = fetchMock.mock.calls.at(-1) as [string];
    expect(pageFromUrl(lastCall[0])).toBe(1);
    expect(new URL(lastCall[0]).searchParams.get("language")).toBe("ja");
  }, TEST_TIMEOUT_MS);

  it("sends the search query and the tag filters as query parameters", async () => {
    // Arrange — 受入基準: 検索語・topics・technologies が API まで届く（Issue #91）
    const fetchMock = stubListPages();

    // Act
    renderHook(() =>
      useInterestArticles({
        ...EMPTY_ARTICLE_FILTERS,
        q: "Rust",
        topics: ["LLM", "RAG"],
        technologies: ["Python"],
      }),
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.get("q")).toBe("Rust");
    expect(searchParams.getAll("topics")).toEqual(["LLM", "RAG"]);
    expect(searchParams.getAll("technologies")).toEqual(["Python"]);
  }, TEST_TIMEOUT_MS);

  it("removes the article from the list immediately when removeArticle is called", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse(listResponse([itemA, itemB]));
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

  it("decreases the total count when an article is removed", async () => {
    // Arrange — 1件だけの一覧。除外すると総件数も0にならなければ、表示側が
    // 「該当する記事がありません」ではなく「他のページを開いてください」を出す
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse(listResponse([itemA]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useInterestArticles(EMPTY_ARTICLE_FILTERS));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.totalCount).toBe(1);

    // Act
    act(() => {
      result.current.removeArticle(itemA.article_id);
    });

    // Assert — 総件数と総ページ数が再取得を待たずに追随する
    expect(result.current.totalCount).toBe(0);
    expect(result.current.totalPages).toBe(0);
  }, TEST_TIMEOUT_MS);

  it("rolls back and surfaces an error when the removal request fails", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response("boom", { status: 500 });
      }
      return jsonResponse(listResponse([itemA, itemB]));
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
    // 総件数も巻き戻す（items だけ戻すと表示が食い違う）
    expect(result.current.totalCount).toBe(2);
  }, TEST_TIMEOUT_MS);

  it("ignores a second removeArticle call on the same article while the request is in flight", async () => {
    // Arrange
    let deleteCallCount = 0;
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        deleteCallCount += 1;
        return new Response(null, { status: 204 });
      }
      return jsonResponse(listResponse([itemA]));
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
