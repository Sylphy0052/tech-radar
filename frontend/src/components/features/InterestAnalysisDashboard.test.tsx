import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InterestAnalysisDashboard } from "@/components/features/InterestAnalysisDashboard";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

const filledSummary = {
  genres: [{ domain: "qiita.com", positive_count: 3, negative_count: 1 }],
  feedback_ratio: { good_count: 5, bad_count: 2, save_count: 1 },
  technologies: [{ technology: "Rust", count: 4 }],
  primary_source_ratio: { primary_count: 4, secondary_count: 6 },
  content_types: [{ content_type: "concept", count: 4 }],
  difficulties: [{ difficulty: "beginner", count: 4 }],
  suppressed_topics: [{ topic: "広告", negative_weight: 0.8, effective_weight: -0.2 }],
};

const filledClusters = {
  items: [{ label: "生成AI", weight: 0.7, topics: ["llm"], updated_at: "2026-08-01T00:00:00Z" }],
};

const filledTimeline = {
  buckets: [
    {
      week_start: "2026-07-27T00:00:00Z",
      interest_article_count: 3,
      topics: [{ topic: "llm", positive_count: 2, negative_count: 0 }],
    },
  ],
};

function stubAllEndpoints(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("/api/interests/clusters")) {
        return jsonResponse(filledClusters);
      }
      if (url.includes("/api/interests/timeline")) {
        return jsonResponse(filledTimeline);
      }
      if (url.includes("/api/interests/summary")) {
        return jsonResponse(filledSummary);
      }
      throw new Error(`unexpected url: ${url}`);
    }),
  );
}

const CHART_HEADINGS = [
  "ジャンル別関心度",
  "Good/Bad比率",
  "関心の時間変化",
  "複数の関心クラスタ",
  "よく読む企業・OSS・技術",
  "公式情報と解説記事の比率",
  "概念・実装・研究・ニュースの比率",
  "難易度の分布",
  "抑制中のジャンル",
];

describe("InterestAnalysisDashboard", () => {
  it("shows a loading indicator before data arrives", () => {
    // Arrange & Act
    stubAllEndpoints();
    render(<InterestAnalysisDashboard />);

    // Assert
    expect(screen.getByRole("status")).toHaveTextContent("読み込み中...");
  });

  it("renders all 9 visualizations once data has loaded", async () => {
    // Arrange
    stubAllEndpoints();

    // Act
    render(<InterestAnalysisDashboard />);

    // Assert
    for (const heading of CHART_HEADINGS) {
      expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    }
  });

  it("shows an error message when a request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    render(<InterestAnalysisDashboard />);

    // Assert
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "通信に失敗しました。しばらくしてから再度お試しください。",
    );
  });
});
