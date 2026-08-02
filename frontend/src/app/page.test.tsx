import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import Home from "@/app/page";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Home", () => {
  it("renders the service name as the heading", () => {
    // Arrange — DiscoverFeed がマウント時にフィードを取得するため、実ネットワーク
    // 呼び出しを避けるよう fetch をスタブする。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })),
    );

    // Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("TechRadar");
  });

  it("renders a link to the interest article list page", () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })),
    );

    // Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("link", { name: "関心記事一覧を見る" })).toHaveAttribute(
      "href",
      "/articles",
    );
  });

  it("renders a link to the interest analysis page", () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ items: [], next_cursor: null })),
    );

    // Act
    render(<Home />);

    // Assert
    expect(screen.getByRole("link", { name: "関心分析を見る" })).toHaveAttribute("href", "/interests");
  });
});
