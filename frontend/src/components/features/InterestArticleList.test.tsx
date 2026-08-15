import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArticleFilterPanel } from "@/components/features/ArticleFilterPanel";
import { InterestArticleList } from "@/components/features/InterestArticleList";
import type { InterestArticleItem } from "@/lib/interest-articles";
import { MAX_PAGE } from "@/lib/pagination";
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
    analysis_status: "completed",
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
    technologies: [],
    title: "Title",
    topics: [],
    translated_title: null,
    ...overrides,
  };
}

function makeItems(count: number, prefix: string): InterestArticleItem[] {
  return Array.from({ length: count }, (_, index) =>
    makeItem({ article_id: `${prefix}-${String(index).padStart(4, "0")}` }),
  );
}

/** URL の `page` クエリ（省略時は 1）を読む（`DiscoverFeed.test.tsx` / `useFeed.test.ts` と同じ）。 */
function pageFromUrl(url: string): number {
  const page = new URL(url).searchParams.get("page");
  return page ? Number(page) : 1;
}

/**
 * どのページを要求されても同じ件数の items を返す fetch のスタブ（Issue #100）。
 *
 * ページングの検証では「どのページが要求されたか」だけを見たいので、記事の中身は
 * ページごとに変えない（`DiscoverFeed.test.tsx` の `stubFeedPages` と同じ狙い）。
 */
function stubArticlePages({
  totalPages = 2,
  totalCount = 4,
}: { totalPages?: number; totalCount?: number } = {}): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    const page = pageFromUrl(url);
    return jsonResponse(
      listResponse(makeItems(2, `page${page}`), {
        total_count: totalCount,
        page,
        page_size: 2,
        total_pages: totalPages,
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * ブラウザ URL とナビゲーションの種別をテストから読むためのプローブ
 * （`DiscoverFeed.test.tsx` と同じ、Issue #100）。
 *
 * `InterestArticleList` はページ移動で URL を書き換えるが、その結果は fetch の
 * URL からは分からない（1ページ目では `page` を URL に出さないが、API へは
 * `page=1` を送る）。ブラウザ側の URL そのものを見る必要があるため、同じ
 * Provider の下に置いて覗く。
 */
function NavigationProbe() {
  const { searchParams, navigations } = useNavigationTestContext();
  return (
    <span
      data-testid="navigation-probe"
      data-query={searchParams.toString()}
      data-kinds={navigations.map((navigation) => navigation.kind).join(",")}
    />
  );
}

function browserQuery(): string {
  return screen.getByTestId("navigation-probe").getAttribute("data-query") ?? "";
}

function navigationKinds(): string {
  return screen.getByTestId("navigation-probe").getAttribute("data-kinds") ?? "";
}

function renderList(initialSearch = ""): ReturnType<typeof render> {
  return render(
    <NavigationTestProvider initialSearch={initialSearch}>
      <InterestArticleList />
      <NavigationProbe />
    </NavigationTestProvider>,
  );
}

/**
 * `ArticleFilterPanel` と `InterestArticleList` を同じ URL の下に併置する
 * （実際の `app/articles/page.tsx` と同じ構成）。フィルター変更で `page` が
 * URL から落ちることを検証するには、フィルターを書き換える側（`ArticleFilterPanel`）
 * も同じ `NavigationTestProvider` の下に無いと URL が繋がらない。
 */
function renderListWithFilterPanel(initialSearch = ""): ReturnType<typeof render> {
  return render(
    <NavigationTestProvider initialSearch={initialSearch}>
      <ArticleFilterPanel />
      <InterestArticleList />
      <NavigationProbe />
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

  // 受入基準（Issue #92）: 技術タグの表示が analysis_status によって状態別に出る。
  // 「機能は動いているのに画面に出ていない」が Issue の実体で、タグが空のとき
  // 「未解析だから空」と「解析済みだが実際に0件」を区別できることが目的。
  it("shows the technology tags when analysis is completed and technologies exist", async () => {
    // Arrange
    const item = makeItem({
      article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "技術タグあり",
      analysis_status: "completed",
      technologies: ["Python", "FastAPI"],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([item]))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("技術タグあり")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("Python")).toBeInTheDocument();
    expect(screen.getByText("FastAPI")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows a pending message when the article has not been analyzed yet", async () => {
    // Arrange — analysis_status が null（古いデータ）でも pending と同じ扱いにする
    const item = makeItem({
      article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "解析待ちの記事",
      analysis_status: null,
      technologies: [],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([item]))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("解析待ちの記事")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("解析待ち")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an analyzing article as pending too", async () => {
    // Arrange
    const item = makeItem({
      article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "解析中の記事",
      analysis_status: "analyzing",
      technologies: [],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([item]))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("解析中の記事")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("解析待ち")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows a failure message when analysis failed", async () => {
    // Arrange
    const item = makeItem({
      article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "解析失敗の記事",
      analysis_status: "failed",
      technologies: [],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([item]))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("解析失敗の記事")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("解析に失敗")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows a no-tags message when analysis is completed but technologies is empty", async () => {
    // Arrange — 「未解析だから空」ではなく「解析済みだが実際に0件」と区別できること
    const item = makeItem({
      article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      title: "タグなしの記事",
      analysis_status: "completed",
      technologies: [],
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(listResponse([item]))));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("タグなしの記事")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("タグなし")).toBeInTheDocument();
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

  it("shows the empty state after the last article on the page is excluded", async () => {
    // Arrange — 総件数1件の一覧。除外後に総件数が古いままだと「他のページを
    // 開いてください」という、存在しないページへの案内が出てしまう
    const item = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title: "最後の1件" });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse(listResponse([item]));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderList();
    await waitFor(() => expect(screen.getByText("最後の1件")).toBeInTheDocument());

    // Act
    fireEvent.click(screen.getByRole("button", { name: "関心対象から除外" }));

    // Assert
    await waitFor(() => expect(screen.getByText("該当する記事がありません。")).toBeInTheDocument());
    expect(screen.getByText("全0件")).toBeInTheDocument();
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

  // 以下、ページ番号を URL クエリへ載せる検証（Issue #100）。
  // `DiscoverFeed.test.tsx` の同名テスト群（フィード側、Issue #95）と対になる。

  it("puts the page number into the URL when a pager button is clicked", async () => {
    // Arrange
    stubArticlePages({ totalPages: 3, totalCount: 6 });
    renderList();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "2ページ目へ" }));

    // Assert — リロードと共有で再現するよう、URL に載る
    await waitFor(() => expect(browserQuery()).toBe("page=2"));
  }, TEST_TIMEOUT_MS);

  // このテストダブルは History スタックを持たないため、「戻ると本当に前のページへ
  // 帰る」ことそのものは見られない。`replace` だと戻れないので、`push` を使っている
  // ことを見るのが、この層で押さえられる一番近いところになる（Issue #95 の背景）。
  it("pushes a new history entry instead of replacing the current one", async () => {
    // Arrange
    stubArticlePages({ totalPages: 3, totalCount: 6 });
    renderList();
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "2ページ目へ" }));

    // Assert
    await waitFor(() => expect(navigationKinds()).toBe("push"));
  }, TEST_TIMEOUT_MS);

  it("does not touch the history when the current page button is clicked", async () => {
    // `Pagination` が無効化するのは「前へ」「次へ」だけで、今見ているページ番号の
    // ボタンは押せる。弾かないと URL が変わらないまま履歴だけが積まれ、戻るボタンを
    // 余計に押さないと前のページへ帰れなくなる。
    // Arrange
    stubArticlePages({ totalPages: 3, totalCount: 6 });
    renderList("page=2");
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act — 今いる2ページ目のボタンをもう一度押す
    fireEvent.click(screen.getByRole("button", { name: "2ページ目へ" }));

    // Assert
    await waitFor(() => expect(browserQuery()).toBe("page=2"));
    expect(navigationKinds()).toBe("");
  }, TEST_TIMEOUT_MS);

  it("opens the page given in the URL on mount", async () => {
    // Arrange
    const fetchMock = stubArticlePages({ totalPages: 3, totalCount: 6 });

    // Act
    renderList("page=3");

    // Assert — 共有された URL を開いた側にも同じページが出る
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(pageFromUrl(url)).toBe(3);
  }, TEST_TIMEOUT_MS);

  it("keeps the page number out of the URL on the first page", async () => {
    // Arrange
    stubArticlePages({ totalPages: 3, totalCount: 6 });
    renderList("page=2");
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "1ページ目へ" }));

    // Assert — 既定値は URL に出さない（他の絞り込み条件と同じ扱い）
    await waitFor(() => expect(browserQuery()).toBe(""));
  }, TEST_TIMEOUT_MS);

  it("keeps the other filters in the URL when the page changes", async () => {
    // Arrange
    stubArticlePages({ totalPages: 3, totalCount: 6 });
    renderList("domain=ai");
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "2ページ目へ" }));

    // Assert — ページ移動で絞り込み条件を落とさない
    await waitFor(() => {
      const query = new URLSearchParams(browserQuery());
      expect(query.get("domain")).toBe("ai");
      expect(query.get("page")).toBe("2");
    });
  }, TEST_TIMEOUT_MS);

  it("drops the page number from the URL when the filters change", async () => {
    // Arrange — `ArticleFilterPanel` は `InterestArticleList` の兄弟としてレンダー
    // される（`DiscoverFeed` が内部で `FeedFilterPanel` を抱えるのと構造が違う、
    // `app/articles/page.tsx` と同じ構成）。
    stubArticlePages({ totalPages: 3, totalCount: 6 });
    renderListWithFilterPanel("page=3");
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));

    // Act
    fireEvent.change(screen.getByLabelText("検索語"), { target: { value: "rust" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert — 条件を変えたら1ページ目へ戻る（絞り込みで件数が減ると
    // 3ページ目が存在しなくなるため）
    await waitFor(() => {
      const query = new URLSearchParams(browserQuery());
      expect(query.get("q")).toBe("rust");
      expect(query.get("page")).toBeNull();
    });
  }, TEST_TIMEOUT_MS);

  // 共有リンク・履歴・手動編集で URL のクエリは容易に壊れる。そのまま
  // `GET /api/articles` へ送ると 422 になるので、1ページ目へ落として表示は成立させる。
  it.each([
    ["a negative page", "page=-1"],
    ["a fractional page", "page=1.5"],
    ["a non-numeric page", "page=abc"],
    ["a page above the backend upper bound", `page=${MAX_PAGE + 1}`],
  ])("shows the first page for a URL with %s", async (_label, search) => {
    // Arrange
    const fetchMock = stubArticlePages({ totalPages: 3, totalCount: 6 });

    // Act
    renderList(search);

    // Assert
    await waitFor(() => expect(screen.getAllByRole("article")).toHaveLength(2));
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(pageFromUrl(url)).toBe(1);
  }, TEST_TIMEOUT_MS);
});
