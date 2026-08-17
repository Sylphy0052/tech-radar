import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Home from "@/app/page";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

// `DiscoverFeed` は URL クエリからフィルターを読む（Issue #90）。この画面のテストは
// フィルターの中身を問わないため、空のクエリと何もしないルーターを返す最小の
// モックで足りる（条件ごとの挙動は DiscoverFeed.test.tsx が見る）。
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
  useRouter: () => ({ replace: () => undefined, push: () => undefined }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

/** 記事0件のフィード応答（`GET /api/feed` は番号付きページングを返す）。 */
function emptyFeedResponse(): Response {
  return jsonResponse({ items: [], page: 1, page_size: 20, total_count: 0, total_pages: 0 });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Home", () => {
  it("renders the service name as the heading", () => {
    // Arrange — DiscoverFeed がマウント時にフィードを取得するため、実ネットワーク
    // 呼び出しを避けるよう fetch をスタブする。
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(emptyFeedResponse()));

    // Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("TechRadar");
  }, TEST_TIMEOUT_MS);

  it("renders a link to the interest article list page", () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(emptyFeedResponse()));

    // Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("link", { name: /関心記事一覧を見る/ })).toHaveAttribute(
      "href",
      "/articles",
    );
  }, TEST_TIMEOUT_MS);

  it("renders a link to the interest analysis page", () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(emptyFeedResponse()));

    // Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("link", { name: /関心分析を見る/ })).toHaveAttribute(
      "href",
      "/interests",
    );
  }, TEST_TIMEOUT_MS);
});
