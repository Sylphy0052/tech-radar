import { act, configure, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useFeed } from "@/hooks/useFeed";
import { EMPTY_FEED_FILTERS } from "@/lib/feed";
import type { FeedFilters, FeedItem } from "@/lib/feed";
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

function makeItem(overrides: Partial<FeedItem> & { article_id: string }): FeedItem {
  return {
    canonical_url: `https://example.com/${overrides.article_id}`,
    original_url: `https://example.com/${overrides.article_id}`,
    feedback: null,
    is_primary_source: true,
    is_read: false,
    language: "en",
    published_at: "2026-08-01T00:00:00Z",
    rank: 1,
    reasons: {},
    score: 0.9,
    source_domain: "example.com",
    summary_ja: "要約",
    technologies: [],
    title: "Title",
    topics: [],
    translated_title: null,
    ...overrides,
  };
}

const itemA = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" });
const itemB = makeItem({ article_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" });
const itemC = makeItem({ article_id: "cccccccc-cccc-cccc-cccc-cccccccccccc" });

const FILTERS_A: FeedFilters = EMPTY_FEED_FILTERS;
const FILTERS_B: FeedFilters = { ...EMPTY_FEED_FILTERS, q: "rust" };

/** URL の `page` クエリ（省略時は 1）を読む。 */
function pageFromUrl(url: string): number {
  const page = new URL(url).searchParams.get("page");
  return page ? Number(page) : 1;
}

/**
 * ページ番号ごとに固定の items を返す fetch のスタブ。応答に含めない
 * `total_count` / `total_pages` は呼び出し側が固定値として渡す。
 */
function stubFeedPages(
  pages: Record<number, FeedItem[]>,
  { totalPages = 2, totalCount = 4 }: { totalPages?: number; totalCount?: number } = {},
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
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

describe("useFeed", () => {
  it("loads page 1 with the given filters on mount", async () => {
    // Arrange
    const fetchMock = stubFeedPages({ 1: [itemA, itemB] });

    // Act
    const { result } = renderHook(() => useFeed(FILTERS_A));

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
    stubFeedPages({ 1: [itemA, itemB], 2: [itemB, itemC] });
    const { result } = renderHook(() => useFeed(FILTERS_A));
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
    const fetchMock = stubFeedPages({ 1: [itemA], 2: [itemB] });
    const { result } = renderHook(() => useFeed(FILTERS_A));
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

  it("resets to page 1 and refetches when the filters change", async () => {
    // Arrange
    const fetchMock = stubFeedPages({ 1: [itemA], 2: [itemB] });
    const { result, rerender } = renderHook(({ filters }) => useFeed(filters), {
      initialProps: { filters: FILTERS_A },
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    act(() => {
      result.current.setPage(2);
    });
    await waitFor(() => expect(result.current.page).toBe(2));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act — 条件を変える
    rerender({ filters: FILTERS_B });

    // Assert — ページ番号が1へ戻る
    await waitFor(() => expect(result.current.page).toBe(1));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const lastCall = fetchMock.mock.calls.at(-1) as [string];
    expect(pageFromUrl(lastCall[0])).toBe(1);
    expect(new URL(lastCall[0]).searchParams.get("q")).toBe("rust");
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
    const { result } = renderHook(() => useFeed(FILTERS_A));
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

  it("shows an empty item list without an error when the filters match nothing", async () => {
    // Arrange
    stubFeedPages({ 1: [] }, { totalPages: 0, totalCount: 0 });

    // Act
    const { result } = renderHook(() => useFeed(FILTERS_A));

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([]);
    expect(result.current.totalCount).toBe(0);
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("surfaces a network error message when the feed request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    const { result } = renderHook(() => useFeed(FILTERS_A));

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.error).toBe(
      "通信に失敗しました。しばらくしてから再度お試しください。",
    );
  }, TEST_TIMEOUT_MS);

  it("applies feedback optimistically before the request resolves", async () => {
    // Arrange
    let resolvePost!: (response: Response) => void;
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          resolvePost = resolve;
        });
      }
      return jsonResponse({ items: [itemA], total_count: 1, page: 1, page_size: 20, total_pages: 1 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.applyFeedback(itemA.article_id, "good");
    });

    // Assert — POST がまだ解決していない時点でローカル state はすでに更新済み
    expect(result.current.items[0]?.feedback?.action).toBe("good");

    // Cleanup — pending の Promise を解決してテスト終了後に警告が残らないようにする
    resolvePost(
      jsonResponse({ action: "good", reason: null, created_at: "2026-08-01T00:00:00Z" }),
    );
    await waitFor(() => expect(result.current.items[0]?.feedback?.action).toBe("good"));
  }, TEST_TIMEOUT_MS);

  it("rolls back and surfaces an error when the feedback request fails", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Response("boom", { status: 500 });
      }
      return jsonResponse({ items: [itemA], total_count: 1, page: 1, page_size: 20, total_pages: 1 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.applyFeedback(itemA.article_id, "good");
    });

    // Assert
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.items[0]?.feedback).toBeNull();
    expect(result.current.error).toBe(
      "サーバーでエラーが発生しました。しばらくしてから再度お試しください。",
    );
  }, TEST_TIMEOUT_MS);

  it("toggles feedback off (DELETE) when the same action is pressed again", async () => {
    // Arrange
    const withFeedback = makeItem({
      article_id: itemA.article_id,
      feedback: { action: "good", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse({
        items: [withFeedback],
        total_count: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items[0]?.feedback?.action).toBe("good");

    // Act — 既に good が付いている記事へ再度 good を押す
    act(() => {
      result.current.applyFeedback(itemA.article_id, "good");
    });

    // Assert — 即座に取り消し状態になり、DELETE が送信される
    expect(result.current.items[0]?.feedback).toBeNull();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/feedback"),
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  }, TEST_TIMEOUT_MS);

  it("updates the reason instead of toggling off when bad is re-sent with a reason", async () => {
    // Arrange
    const withBad = makeItem({
      article_id: itemA.article_id,
      feedback: { action: "bad", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({
          action: "bad",
          reason: "too_shallow",
          created_at: "2026-08-01T00:00:00Z",
        });
      }
      return jsonResponse({ items: [withBad], total_count: 1, page: 1, page_size: 20, total_pages: 1 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act — 既に理由なしの bad が付いている記事へ、理由を指定して bad を送り直す
    act(() => {
      result.current.applyFeedback(itemA.article_id, "bad", "too_shallow");
    });

    // Assert — 取り消しではなく理由の更新として扱う
    expect(result.current.items[0]?.feedback?.action).toBe("bad");
    await waitFor(() => expect(result.current.items[0]?.feedback?.reason).toBe("too_shallow"));
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ method: "DELETE" }),
    );
  }, TEST_TIMEOUT_MS);

  it("removes feedback via DELETE and clears it locally", async () => {
    // Arrange
    const withFeedback = makeItem({
      article_id: itemA.article_id,
      feedback: { action: "save", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse({
        items: [withFeedback],
        total_count: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.removeFeedback(itemA.article_id);
    });

    // Assert — 即座にローカル state から消え、DELETE が送信される
    expect(result.current.items[0]?.feedback).toBeNull();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/feedback"),
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  }, TEST_TIMEOUT_MS);

  it("does nothing when removeFeedback is called on an article without feedback", async () => {
    // Arrange
    const fetchMock = stubFeedPages({ 1: [itemA] });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.removeFeedback(itemA.article_id);
    });

    // Assert — feedback が無い記事には API を叩かない
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
    // 取り消すものが無いだけの正常な呼び出しなので、警告は出さない（Issue #45）。
    expect(warn).not.toHaveBeenCalled();
  }, TEST_TIMEOUT_MS);

  it("ignores a second click on the same article while a feedback request is in flight (G-1)", async () => {
    // Arrange — 既に good が付いている記事に対し、1 回目のクリックで取り消し（DELETE）が
    // 走っている最中に、再レンダリングを挟まず同じ Good ボタンをもう一度押す想定。
    const withFeedback = makeItem({
      article_id: itemA.article_id,
      feedback: { action: "good", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });
    let deleteCallCount = 0;
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        deleteCallCount += 1;
        if (deleteCallCount > 1) {
          return new Response("not found", { status: 404 });
        }
        return new Response(null, { status: 204 });
      }
      return jsonResponse({
        items: [withFeedback],
        total_count: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act — 再レンダリングを挟まず、同じ articleId に対して連続で applyFeedback を呼ぶ
    act(() => {
      result.current.applyFeedback(itemA.article_id, "good");
      result.current.applyFeedback(itemA.article_id, "good");
    });

    // Assert — 送信中の 2 回目は無視され、DELETE は 1 回だけ送信される
    await waitFor(() => expect(deleteCallCount).toBe(1));
    expect(result.current.items[0]?.feedback).toBeNull();
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("does nothing when applyFeedback targets an unknown article id", async () => {
    // Arrange
    const fetchMock = stubFeedPages({ 1: [itemA] });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed(FILTERS_A));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.applyFeedback("unknown-id", "good");
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
    // 黙って戻らず、開発時には原因を追える痕跡が残る（Issue #45）。
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0]?.[0]).toContain("unknown-id");
  }, TEST_TIMEOUT_MS);

  it("hands out a feedback callback that sees the items already rendered (G-3)", async () => {
    // Arrange — 一覧が出た直後のクリックを取りこぼさないことを固定する。
    stubFeedPages({ 1: [itemA] });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed(FILTERS_A));
    const applyFeedbackWhileEmpty = result.current.applyFeedback;
    expect(result.current.items).toEqual([]);

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const applyFeedbackAfterLoad = result.current.applyFeedback;

    // Assert — items が入れ替わったらハンドラも作り直される
    expect(applyFeedbackAfterLoad).not.toBe(applyFeedbackWhileEmpty);

    // Act — ロード後のハンドラは対象を見つけて楽観的更新する
    act(() => {
      applyFeedbackAfterLoad(itemA.article_id, "good");
    });

    // Assert
    await waitFor(() => expect(result.current.items[0]?.feedback?.action).toBe("good"));
    expect(warn).not.toHaveBeenCalled();
  }, TEST_TIMEOUT_MS);
});
