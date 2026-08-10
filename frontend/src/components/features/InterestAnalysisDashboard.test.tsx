import { configure, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InterestAnalysisDashboard } from "@/components/features/InterestAnalysisDashboard";
import { TEST_TIMEOUT_MS, WAIT_TIMEOUT_MS } from "@/test-utils/timeouts";

/**
 * jsdom 上の recharts の描画はグラフ 1 個あたり 1〜3 秒かかる。7 種のグラフを
 * 一度に描画するのはこのダッシュボードだけで、`TEST_TIMEOUT_MS` を配っても
 * グラフが増えるたびに実行時間が伸び続ける（Issue #41）。
 *
 * このテストが確かめたいのは「3 つの API から取得したデータで 9 種の可視化が
 * 出そろうこと」であり、グラフ内部の描画ではない。内部は実物の recharts を使う
 * 個別のテスト（`InterestGenreChart.test.tsx` など 7 ファイル）が担保している。
 * グラフを包むカード（見出しと空表示の判定）は実装のまま動くため、素通しの
 * `div` へ差し替えてもこのファイルの検証内容は変わらない。
 */
vi.mock("recharts", async () => {
  const { createElement } = await import("react");
  const stub = (name: string) =>
    function RechartsStub({ children }: { children?: ReactNode }) {
      return createElement("div", { "data-recharts-stub": name }, children);
    };
  // 7 つのグラフが recharts から取り込んでいる export の和集合。
  const names = [
    "Bar",
    "BarChart",
    "CartesianGrid",
    "Cell",
    "Legend",
    "Line",
    "LineChart",
    "Pie",
    "PieChart",
    "ResponsiveContainer",
    "Tooltip",
    "XAxis",
    "YAxis",
  ];
  return Object.fromEntries(names.map((name) => [name, stub(name)]));
});

configure({ asyncUtilTimeout: WAIT_TIMEOUT_MS });

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
  }, TEST_TIMEOUT_MS);

  it("renders all 9 visualizations once data has loaded", async () => {
    // Arrange
    stubAllEndpoints();

    // Act
    render(<InterestAnalysisDashboard />);

    // Assert
    for (const heading of CHART_HEADINGS) {
      expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
    }
  }, TEST_TIMEOUT_MS);

  it("shows an error message when a request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    render(<InterestAnalysisDashboard />);

    // Assert
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "通信に失敗しました。しばらくしてから再度お試しください。",
    );
  }, TEST_TIMEOUT_MS);
});
