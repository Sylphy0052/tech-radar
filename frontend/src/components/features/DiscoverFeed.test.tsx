import { act, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DiscoverFeed } from "@/components/features/DiscoverFeed";
import type { FeedItem } from "@/lib/feed";
import { TEST_TIMEOUT_MS, WAIT_TIMEOUT_MS } from "@/test-utils/timeouts";

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

/**
 * センチネルが可視／不可視になったことを通知する。
 *
 * observer がまだ生成されていない場合は明示的に失敗させる。記事の描画（DOM の
 * 変化）と observer を張る effect の実行順は保証されないため、負荷が高いと
 * 通知の時点で observer が無いことがある。ここで黙って何もしないと、
 * 呼び出し側は「通知したのに何も起きない」状態のまま `waitFor` で空回りし、
 * 原因の分からないタイムアウトとして現れる（Issue #35）。
 */
function triggerIntersection(isIntersecting: boolean): void {
  const instance = MockIntersectionObserver.instances.at(-1);
  if (!instance) {
    throw new Error(
      "IntersectionObserver が未生成のまま通知しようとした。" +
        "呼ぶ前に await waitForObserver() で observer が張られるのを待つこと。",
    );
  }
  instance.callback([{ isIntersecting }]);
}

/** observer が張られるまで待つ。`triggerIntersection` の前提を満たすために使う。 */
async function waitForObserver(): Promise<void> {
  await waitFor(() => expect(MockIntersectionObserver.instances).toHaveLength(1));
}

configure({ asyncUtilTimeout: WAIT_TIMEOUT_MS });

beforeEach(() => {
  MockIntersectionObserver.instances = [];
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
  // `Date.now` の固定（レート制限のテスト）を後続のテストへ持ち越さない。
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
    render(<DiscoverFeed />);

    // Assert
    expect(screen.getByRole("status")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("renders 20 article cards from the first page", async () => {
    // Arrange
    const items = makeItems(20, "page1");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ items, next_cursor: null })));

    // Act
    render(<DiscoverFeed />);

    // Assert
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(20));
  }, TEST_TIMEOUT_MS);

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
    // 記事の描画（DOM の変化）と observer を張る effect の実行順は保証されないため、
    // observer が張られるまで待ってから通知する（未生成のまま呼ぶと
    // `triggerIntersection` が例外を投げる）。
    await waitForObserver();

    // Act — センチネルが可視になったことを通知する
    act(() => {
      triggerIntersection(true);
    });

    // Assert — 4 件（重複なし）に増える
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(4));
    const renderedIds = [...page1, ...page2].map((item) => item.article_id);
    expect(new Set(renderedIds).size).toBe(4);
  }, TEST_TIMEOUT_MS);

  it("does not recreate the IntersectionObserver after loading a page that still has more (G-2)", async () => {
    // Arrange — 3 ページ用意し、2 ページ目を読み込んだ後も hasMore が true のまま
    // 変化しない状況を作る（hasMore 自体の変化による observer 再生成と切り分けるため）。
    const page1 = makeItems(2, "page1");
    const page2 = makeItems(2, "page2");
    const page3 = makeItems(2, "page3");
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("cursor=page-3")) {
        return jsonResponse({ items: page3, next_cursor: null });
      }
      if (url.includes("cursor=page-2")) {
        return jsonResponse({ items: page2, next_cursor: "page-3" });
      }
      return jsonResponse({ items: page1, next_cursor: "page-2" });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DiscoverFeed />);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    // 記事の描画（DOM の変化）と observer を張る effect の実行順は保証されないため、
    // observer が張られるまで待ってから起点の件数を確定させる。
    await waitForObserver();

    // Act — 2 ページ目を読み込む（読み込み後も hasMore は true のまま）
    act(() => {
      triggerIntersection(true);
    });
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(4));

    // Assert — observer が作り直されていないこと
    expect(MockIntersectionObserver.instances).toHaveLength(1);
  }, TEST_TIMEOUT_MS);

  it("does not request another page and shows the end message once hasMore is false", async () => {
    // Arrange
    const items = makeItems(1, "only");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items, next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    render(<DiscoverFeed />);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(1));
    const callCountBefore = fetchMock.mock.calls.length;

    // Assert — hasMore が false なら監視自体を始めない（センチネルも描画されない）
    // ため、可視になったことを通知する経路がそもそも存在しない。以前はここで
    // `triggerIntersection` を呼んでいたが、observer が無い状態では何も起きず、
    // 「通知しても追加ロードされない」ことを確かめたつもりで実際には何も
    // 検証できていなかった（Issue #35）。
    expect(MockIntersectionObserver.instances).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "さらに読み込む" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(callCountBefore);
    expect(screen.getByText("すべての記事を読み込みました")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an error message when the feed request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    render(<DiscoverFeed />);

    // Assert
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  }, TEST_TIMEOUT_MS);

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
  }, TEST_TIMEOUT_MS);

  it("keeps loading the next page when a page is empty but more pages remain", async () => {
    // Arrange — ページ内が全件 Bad だと items が空でも next_cursor は返る
    // （バックエンドは cursor を除外前の rank から計算するため）。
    const page2 = makeItems(2, "page2");
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("cursor=page-2")) {
        return jsonResponse({ items: page2, next_cursor: null });
      }
      return jsonResponse({ items: [], next_cursor: "page-2" });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<DiscoverFeed />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "さらに読み込む" })).toBeInTheDocument(),
    );

    // Assert — 空ページを「記事がありません」で打ち切らない
    expect(screen.queryByText("表示できる記事がありません。")).not.toBeInTheDocument();

    // Act — 続きを読む手段が残っている
    fireEvent.click(screen.getByRole("button", { name: "さらに読み込む" }));

    // Assert
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
  }, TEST_TIMEOUT_MS);

  it("shows only the rate limit message when the initial fetch is rejected with 429", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        async () =>
          new Response("too many requests", {
            status: 429,
            headers: { "Retry-After": "30" },
          }),
      ),
    );

    // Act
    render(<DiscoverFeed />);

    // Assert — 一時的な制限を「記事が無い」と誤って伝えない
    await waitFor(() =>
      expect(
        screen.getByText("リクエストが多すぎます。約30秒後に再度お試しください。"),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("表示できる記事がありません。")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("stops loading more pages while the sentinel keeps intersecting after a 429", async () => {
    // Arrange
    const page1 = makeItems(2, "page1");
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("cursor=page-2")) {
        return new Response("too many requests", {
          status: 429,
          headers: { "Retry-After": "30" },
        });
      }
      return jsonResponse({ items: page1, next_cursor: "page-2" });
    });
    vi.stubGlobal("fetch", fetchMock);
    // 429 のあと `useFeed` は残り時間を計算し直してメッセージを出す
    // （`rateLimitedUntilRef - Date.now()`）。実時間で 1 秒以上経つと文言が
    // 「約29秒後」へ変わり、負荷の高い環境でだけ検証が崩れるため時計を止める。
    // `waitFor` のポーリングと打ち切りは setTimeout / MutationObserver で動いて
    // おり `Date.now` を見ないため、完全に固定しても待機は壊れない。
    vi.spyOn(Date, "now").mockReturnValue(new Date("2026-08-03T00:00:00Z").getTime());
    render(<DiscoverFeed />);
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    // 記事の描画と observer を張る effect の実行順は保証されないため、
    // observer が張られるまで待ってから通知する。
    await waitForObserver();

    // Act — センチネルが可視になり続ける（スクロール中の再通知）
    act(() => {
      triggerIntersection(true);
    });
    await waitFor(() =>
      expect(
        screen.getByText("リクエストが多すぎます。約30秒後に再度お試しください。"),
      ).toBeInTheDocument(),
    );
    const callCountAfterRateLimit = fetchMock.mock.calls.length;
    act(() => {
      triggerIntersection(true);
    });
    fireEvent.click(screen.getByRole("button", { name: "さらに読み込む" }));

    // Assert — 再試行せず、待機中である旨の表示は残る
    expect(fetchMock).toHaveBeenCalledTimes(callCountAfterRateLimit);
    expect(
      screen.getByText("リクエストが多すぎます。約30秒後に再度お試しください。"),
    ).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

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

    // Assert — API 応答を待たずに押下状態になる。POST のモックは解決しない Promise を
    // 返すため、押下状態になるのは楽観的更新によるものだけ（応答による反映は起こらない）。
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Good" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
  }, TEST_TIMEOUT_MS);

  it("marks the Good button as pressed even when clicked in the same tick it appears (Issue #37)", async () => {
    // Arrange — 記事が DOM に出た瞬間（React の passive effect が走る前）に
    // クリックする。MutationObserver のコールバックはマイクロタスク、passive
    // effect はマクロタスク（MessageChannel）で走るため、この順序は必ず成立する。
    // 以前はこの窓でクリックすると、hook が最新の items をまだ読めず、操作が
    // 黙って捨てられていた（Issue #37）。
    const items = makeItems(1, "only");
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Promise<Response>(() => {});
      }
      return jsonResponse({ items, next_cursor: null });
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
      render(<DiscoverFeed />);

      await waitFor(() => expect(clicked).toBe(true));

      // Assert — API 応答を待たずに押下状態になる（描画直後のクリックが
      // 黙って捨てられない）
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
