import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InterestArticleList } from "@/components/features/InterestArticleList";
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

/** 1ページに収まる関心記事一覧レスポンス（番号付きページング、Issue #91）。 */
function listResponse(items: InterestArticleItem[], overrides: Record<string, unknown> = {}): unknown {
  return {
    items,
    total_count: items.length,
    page: 1,
    page_size: 20,
    total_pages: items.length > 0 ? 1 : 0,
    ...overrides,
  };
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

function renderList(initialSearch = ""): ReturnType<typeof render> {
  return render(
    <NavigationTestProvider initialSearch={initialSearch}>
      <InterestArticleList />
    </NavigationTestProvider>,
  );
}

describe("InterestArticleList", () => {
  it("shows the origin label for manual/good/saved articles", async () => {
    // Arrange
    const items = [
      makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", origin: "manual", title: "手動記事" }),
      makeItem({ article_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", origin: "good", title: "Good記事" }),
      makeItem({ article_id: "cccccccc-cccc-cccc-cccc-cccccccccccc", origin: "saved", title: "保存記事" }),
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse(items))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("手動記事")).toBeInTheDocument());

    // Assert
    const manualCard = screen.getByText("手動記事").closest("article");
    const goodCard = screen.getByText("Good記事").closest("article");
    const savedCard = screen.getByText("保存記事").closest("article");
    expect(manualCard).not.toBeNull();
    expect(goodCard).not.toBeNull();
    expect(savedCard).not.toBeNull();
    expect(within(manualCard as HTMLElement).getByText("手動登録")).toBeInTheDocument();
    expect(within(goodCard as HTMLElement).getByText("Good")).toBeInTheDocument();
    expect(within(savedCard as HTMLElement).getByText("保存")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows registered_at, topics, source_domain and content_type", async () => {
    // Arrange
    const item = makeItem({
      article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "記事タイトル",
      registered_at: "2026-08-01T12:00:00Z",
      topics: ["llm"],
      source_domain: "blog.example.com",
      content_type: "research",
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([item]))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("記事タイトル")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("llm")).toBeInTheDocument();
    expect(screen.getByText("blog.example.com")).toBeInTheDocument();
    expect(screen.getByText("研究・論文")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("removes the article from the list once it is excluded", async () => {
    // Arrange
    const item = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title: "除外対象" });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse(listResponse([item]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderList();
    await waitFor(() => expect(screen.getByText("除外対象")).toBeInTheDocument());

    // Act — 素の DOM の click() は act の外で状態更新が走り、負荷時に反映を取りこぼす。
    fireEvent.click(screen.getByRole("button", { name: "関心対象から除外" }));

    // Assert
    await waitFor(() => expect(screen.queryByText("除外対象")).not.toBeInTheDocument());
  }, TEST_TIMEOUT_MS);

  it("removes the article even when clicked in the same tick it appears (Issue #37)", async () => {
    // Arrange — 記事が DOM に出た瞬間（React の passive effect が走る前）に
    // クリックする。MutationObserver のコールバックはマイクロタスク、passive
    // effect はマクロタスク（MessageChannel）で走るため、この順序は必ず成立する
    // （React のスケジューリング実装に依存する前提のため、React の major 更新時は
    // この再現が生きているかを確認すること）。
    // 以前はこの窓でクリックすると、hook が最新の items をまだ読めず、操作が
    // 黙って捨てられていた（Issue #37）。
    const item = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title: "除外対象" });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse(listResponse([item]));
    });
    vi.stubGlobal("fetch", fetchMock);

    let clicked = false;
    const observer = new MutationObserver(() => {
      if (clicked) {
        return;
      }
      const button = screen.queryByRole("button", { name: "関心対象から除外" });
      if (button === null) {
        return;
      }
      clicked = true;
      fireEvent.click(button);
    });
    observer.observe(document.body, { childList: true, subtree: true });

    try {
      // Act
      renderList();

      await waitFor(() => expect(clicked).toBe(true));

      // Assert — 描画直後のクリックが黙って捨てられず、除外が反映される
      await waitFor(() => expect(screen.queryByText("除外対象")).not.toBeInTheDocument());
    } finally {
      observer.disconnect();
    }
  }, TEST_TIMEOUT_MS);

  it("shows an error message when the request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    renderList();

    // Assert
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("通信に失敗しました");
  }, TEST_TIMEOUT_MS);

  it("shows an empty state message when there are no matching articles", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([]))));

    // Act
    renderList();

    // Assert
    await waitFor(() => expect(screen.getByText("該当する記事がありません。")).toBeInTheDocument());
  }, TEST_TIMEOUT_MS);

  it("requests the API with the filters restored from the URL", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(listResponse([])));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    renderList("origin=good&domain=ai");

    // Assert
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.getAll("origin")).toEqual(["good"]);
    expect(searchParams.get("domain")).toBe("ai");
  }, TEST_TIMEOUT_MS);

  it("moves to the requested page and replaces the shown articles", async () => {
    // Arrange — 2ページ分の応答を用意する（受入基準: 番号付きページング）
    const firstPageItem = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title: "1ページ目の記事" });
    const secondPageItem = makeItem({ article_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", title: "2ページ目の記事" });
    const fetchMock = vi.fn().mockImplementation(async (url: string) => {
      const page = Number(new URL(url).searchParams.get("page") ?? "1");
      return jsonResponse(
        listResponse(page === 2 ? [secondPageItem] : [firstPageItem], {
          total_count: 2,
          page,
          page_size: 1,
          total_pages: 2,
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderList();
    await waitFor(() => expect(screen.getByText("1ページ目の記事")).toBeInTheDocument());

    // Act
    fireEvent.click(screen.getByRole("button", { name: "次のページへ" }));

    // Assert — 追記ではなく差し替わる
    await waitFor(() => expect(screen.getByText("2ページ目の記事")).toBeInTheDocument());
    expect(screen.queryByText("1ページ目の記事")).not.toBeInTheDocument();
    const lastCall = fetchMock.mock.calls.at(-1) as [string];
    expect(new URL(lastCall[0]).searchParams.get("page")).toBe("2");
  }, TEST_TIMEOUT_MS);

  it("requests the API with the search query and the tag filters restored from the URL", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(listResponse([])));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    renderList("q=Rust&topics=LLM&topics=RAG&technologies=Python");

    // Assert
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.get("q")).toBe("Rust");
    expect(searchParams.getAll("topics")).toEqual(["LLM", "RAG"]);
    expect(searchParams.getAll("technologies")).toEqual(["Python"]);
  }, TEST_TIMEOUT_MS);
});
