import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InterestArticleList } from "@/components/features/InterestArticleList";
import type { InterestArticleItem } from "@/lib/interest-articles";
import { NavigationTestProvider, useNavigationTestContext } from "@/test-utils/next-navigation-test-context";

vi.mock("next/navigation", () => ({
  useSearchParams: () => useNavigationTestContext().searchParams,
  usePathname: () => useNavigationTestContext().pathname,
  useRouter: () => useNavigationTestContext().router,
}));

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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ items, next_cursor: null })));

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
  });

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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ items: [item], next_cursor: null })));

    // Act
    renderList();
    await waitFor(() => expect(screen.getByText("記事タイトル")).toBeInTheDocument());

    // Assert
    expect(screen.getByText("llm")).toBeInTheDocument();
    expect(screen.getByText("blog.example.com")).toBeInTheDocument();
    expect(screen.getByText("研究・論文")).toBeInTheDocument();
  });

  it("removes the article from the list once it is excluded", async () => {
    // Arrange
    const item = makeItem({ article_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", title: "除外対象" });
    const fetchMock = vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return new Response(null, { status: 204 });
      }
      return jsonResponse({ items: [item], next_cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderList();
    await waitFor(() => expect(screen.getByText("除外対象")).toBeInTheDocument());

    // Act
    screen.getByRole("button", { name: "関心対象から除外" }).click();

    // Assert
    await waitFor(() => expect(screen.queryByText("除外対象")).not.toBeInTheDocument());
  });

  it("shows an error message when the request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    renderList();

    // Assert
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent("通信に失敗しました");
  });

  it("shows an empty state message when there are no matching articles", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })));

    // Act
    renderList();

    // Assert
    await waitFor(() => expect(screen.getByText("該当する記事がありません。")).toBeInTheDocument());
  });

  it("requests the API with the filters restored from the URL", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    renderList("origin=good&domain=ai");

    // Assert
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url] = fetchMock.mock.calls[0] as [string];
    const searchParams = new URL(url).searchParams;
    expect(searchParams.getAll("origin")).toEqual(["good"]);
    expect(searchParams.get("domain")).toBe("ai");
  });
});
