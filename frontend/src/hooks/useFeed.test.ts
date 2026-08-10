import { act, configure, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useFeed } from "@/hooks/useFeed";
import type { FeedItem } from "@/lib/feed";
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

function stubFeedPages(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (url.includes("cursor=page-2")) {
      // ページ間で重複が出ないことの検証用に、あえて 1 件だけ前ページと重複させる。
      return jsonResponse({ items: [itemB, itemC], next_cursor: null });
    }
    return jsonResponse({ items: [itemA, itemB], next_cursor: "page-2" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** 2 ページ目だけ 429（Retry-After: 30）を返す fetch を差し込む。 */
function stubRateLimitedSecondPage(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (url.includes("cursor=")) {
      return new Response("too many requests", {
        status: 429,
        headers: { "Retry-After": "30" },
      });
    }
    return jsonResponse({ items: [itemA], next_cursor: "page-2" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * `Date.now()` を「実時間 + 任意のオフセット」にする。
 *
 * fake timers で時刻ごと止めると `waitFor` のポーリングが進まなくなるため、
 * 実時間はそのまま進めたうえで先送りだけを足せるようにしている。
 */
function stubAdvanceableClock(): { advance: (ms: number) => void; restore: () => void } {
  const realNow = Date.now.bind(Date);
  let offsetMs = 0;
  const spy = vi.spyOn(Date, "now").mockImplementation(() => realNow() + offsetMs);
  return {
    advance: (ms: number) => {
      offsetMs += ms;
    },
    restore: () => {
      spy.mockRestore();
    },
  };
}

describe("useFeed", () => {
  it("loads the first page on mount", async () => {
    // Arrange
    stubFeedPages();

    // Act
    const { result } = renderHook(() => useFeed());

    // Assert
    expect(result.current.isLoading).toBe(true);
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.items).toEqual([itemA, itemB]);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("appends the next page without duplicating articles already present", async () => {
    // Arrange
    stubFeedPages();
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));

    // Assert — itemB は両ページに含まれるが 1 回だけ現れる
    expect(result.current.items.map((item) => item.article_id)).toEqual([
      itemA.article_id,
      itemB.article_id,
      itemC.article_id,
    ]);
    expect(result.current.hasMore).toBe(false);
  }, TEST_TIMEOUT_MS);

  it("does not call the API again once next_cursor is null", async () => {
    // Arrange
    const fetchMock = stubFeedPages();
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
    expect(result.current.hasMore).toBe(false);
    const callCountAfterExhausted = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.loadMore();
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountAfterExhausted);
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
      return jsonResponse({ items: [itemA], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
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
      return jsonResponse({ items: [itemA], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
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
      return jsonResponse({ items: [withFeedback], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
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
      return jsonResponse({ items: [withBad], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
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
      return jsonResponse({ items: [withFeedback], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
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

  it("ignores a second removeFeedback call on the same article while the request is in flight", async () => {
    // Arrange — applyFeedback と同じ pending ガードが removeFeedback にも
    // 効いていることを確認する。
    const withFeedback = makeItem({
      article_id: itemA.article_id,
      feedback: { action: "save", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });
    let deleteCallCount = 0;
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        deleteCallCount += 1;
        return new Response(null, { status: 204 });
      }
      return jsonResponse({ items: [withFeedback], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act — 再レンダリングを挟まず、同じ articleId に対して連続で removeFeedback を呼ぶ
    act(() => {
      result.current.removeFeedback(itemA.article_id);
      result.current.removeFeedback(itemA.article_id);
    });

    // Assert — 送信中の 2 回目は無視され、DELETE は 1 回だけ送信される
    await waitFor(() => expect(deleteCallCount).toBe(1));
  }, TEST_TIMEOUT_MS);

  it("does nothing when removeFeedback is called on an article without feedback", async () => {
    // Arrange
    const fetchMock = stubFeedPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed());
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

  it("does nothing when removeFeedback targets an unknown article id", async () => {
    // Arrange — applyFeedback 版と対になる。一覧に無い article_id を渡された場合は
    // 黙って戻らず痕跡を残す（Issue #45）。
    const fetchMock = stubFeedPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const callCountBefore = fetchMock.mock.calls.length;

    // Act
    act(() => {
      result.current.removeFeedback("unknown-id");
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0]?.[0]).toContain("unknown-id");
  }, TEST_TIMEOUT_MS);

  it("rolls back and surfaces an error when removeFeedback's DELETE request fails", async () => {
    // Arrange
    const withFeedback = makeItem({
      article_id: itemA.article_id,
      feedback: { action: "save", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response("boom", { status: 500 });
      }
      return jsonResponse({ items: [withFeedback], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.removeFeedback(itemA.article_id);
    });

    // Assert
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.items[0]?.feedback).toEqual(withFeedback.feedback);
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
          // 既に削除済みのものへの 2 回目の DELETE はサーバー側で 404 になる想定。
          // 修正前の実装ではこの 404 が catch のロールバックを誘発し、
          // 消したはずの feedback が復活してしまっていた。
          return new Response("not found", { status: 404 });
        }
        return new Response(null, { status: 204 });
      }
      return jsonResponse({ items: [withFeedback], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => useFeed());
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

  it("keeps the loadMore reference stable while a fetch is in flight (G-2)", async () => {
    // Arrange
    stubFeedPages();
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const loadMoreBeforeFetch = result.current.loadMore;

    // Act
    act(() => {
      result.current.loadMore();
    });
    const loadMoreDuringFetch = result.current.loadMore;
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
    const loadMoreAfterFetch = result.current.loadMore;

    // Assert — isLoadingMore / nextCursor の変化のたびに参照が変わっていないこと
    // （変わると呼び出し側の useEffect が毎回 IntersectionObserver を作り直してしまう）
    expect(loadMoreDuringFetch).toBe(loadMoreBeforeFetch);
    expect(loadMoreAfterFetch).toBe(loadMoreBeforeFetch);
  }, TEST_TIMEOUT_MS);

  it("does nothing when applyFeedback targets an unknown article id", async () => {
    // Arrange
    const fetchMock = stubFeedPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed());
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
    // Issue #45 の失敗は、ハンドラが `useEffect` 経由のミラー（itemsRef）から
    // items を読んでいたために「DOM には記事が出ているがミラーはまだ空」という
    // 窓を踏み、楽観的更新も API 送信もせずに戻っていた形だった。
    // ハンドラが最新の items を見ることを直接固定しておけば、依存配列を
    // 空へ戻す（＝古いクロージャを掴む）退行をここで検出できる。
    stubFeedPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed());
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
    await waitFor(() =>
      expect(result.current.items[0]?.feedback?.action).toBe("good"),
    );
    expect(warn).not.toHaveBeenCalled();
  }, TEST_TIMEOUT_MS);

  it("warns instead of silently dropping the click when a stale callback is used (G-3)", async () => {
    // Arrange — items が空だった頃のハンドラを、ロード後に呼ぶ。
    // これは itemsRef 方式が踏んでいた窓そのものの再現であり、握り潰さずに
    // 痕跡が残ることを固定する。
    stubFeedPages();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { result } = renderHook(() => useFeed());
    const applyFeedbackWhileEmpty = result.current.applyFeedback;
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      applyFeedbackWhileEmpty(itemA.article_id, "good");
    });

    // Assert
    expect(result.current.items[0]?.feedback).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0]?.[0]).toContain(itemA.article_id);
  }, TEST_TIMEOUT_MS);

  it("surfaces a rate limit message with the wait time when loadMore hits 429", async () => {
    // Arrange
    stubRateLimitedSecondPage();
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    // Act
    act(() => {
      result.current.loadMore();
    });

    // Assert
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
    expect(result.current.error).toBe("リクエストが多すぎます。約30秒後に再度お試しください。");
  }, TEST_TIMEOUT_MS);

  it("does not retry immediately after a 429 response", async () => {
    // Arrange
    const fetchMock = stubRateLimitedSecondPage();
    const { result } = renderHook(() => useFeed());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    act(() => {
      result.current.loadMore();
    });
    await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
    const callCountAfterRateLimit = fetchMock.mock.calls.length;

    // Act — 無限スクロールの再交差や「さらに読み込む」連打を模す
    act(() => {
      result.current.loadMore();
      result.current.loadMore();
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(callCountAfterRateLimit);
  }, TEST_TIMEOUT_MS);

  it("re-shows the rate limit message with the remaining wait when loadMore is pressed during the cooldown", async () => {
    // Arrange — 429 のあとフィードバック送信が成功してエラー表示が消える状況
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ action: "good", reason: null, created_at: "2026-08-01T00:00:00Z" });
      }
      if (url.includes("cursor=")) {
        return new Response("too many requests", {
          status: 429,
          headers: { "Retry-After": "30" },
        });
      }
      return jsonResponse({ items: [itemA], next_cursor: "page-2" });
    });
    vi.stubGlobal("fetch", fetchMock);
    const clock = stubAdvanceableClock();
    try {
      const { result } = renderHook(() => useFeed());
      await waitFor(() => expect(result.current.isLoading).toBe(false));
      act(() => {
        result.current.loadMore();
      });
      await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
      act(() => {
        result.current.applyFeedback(itemA.article_id, "good");
      });
      await waitFor(() => expect(result.current.error).toBeNull());

      // Act — 25 秒経過した時点で「さらに読み込む」を押す
      clock.advance(25_000);
      act(() => {
        result.current.loadMore();
      });

      // Assert — 押しても何も起きないのではなく、残りの待ち時間を出し直す
      expect(result.current.error).toBe("リクエストが多すぎます。約5秒後に再度お試しください。");
    } finally {
      clock.restore();
    }
  }, TEST_TIMEOUT_MS);

  it("allows loadMore again once the Retry-After window has elapsed", async () => {
    // Arrange
    const fetchMock = stubRateLimitedSecondPage();
    const clock = stubAdvanceableClock();
    try {
      const { result } = renderHook(() => useFeed());
      await waitFor(() => expect(result.current.isLoading).toBe(false));
      act(() => {
        result.current.loadMore();
      });
      await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
      const callCountAfterRateLimit = fetchMock.mock.calls.length;

      // Act
      clock.advance(30_000);
      act(() => {
        result.current.loadMore();
      });

      // Assert
      await waitFor(() => expect(result.current.isLoadingMore).toBe(false));
      expect(fetchMock.mock.calls.length).toBe(callCountAfterRateLimit + 1);
    } finally {
      clock.restore();
    }
  }, TEST_TIMEOUT_MS);

  it("surfaces a network error message when the feed request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    const { result } = renderHook(() => useFeed());

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.error).toBe(
      "通信に失敗しました。しばらくしてから再度お試しください。",
    );
  }, TEST_TIMEOUT_MS);
});
