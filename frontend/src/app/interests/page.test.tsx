import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import InterestsPage from "@/app/interests/page";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("InterestsPage", () => {
  it("renders the heading and description", () => {
    // Arrange — InterestAnalysisDashboard がマウント時に3本の GET
    // （summary/clusters/timeline）を発行するため、URL ごとに対応する形の
    // レスポンスをスタブする（実ネットワーク呼び出しを避けるため）。
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: string) => {
        if (url.includes("/api/interests/clusters")) {
          return jsonResponse({ items: [] });
        }
        if (url.includes("/api/interests/timeline")) {
          return jsonResponse({ buckets: [] });
        }
        return jsonResponse({
          genres: [],
          feedback_ratio: { good_count: 0, bad_count: 0, save_count: 0 },
          technologies: [],
          primary_source_ratio: { primary_count: 0, secondary_count: 0 },
          content_types: [],
          difficulties: [],
          suppressed_topics: [],
        });
      }),
    );

    // Act
    render(<InterestsPage />);

    // Assert
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("関心分析");
  });
});
