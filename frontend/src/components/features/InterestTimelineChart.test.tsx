import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestTimelineChart } from "@/components/features/InterestTimelineChart";
import type { InterestTimelineBucket } from "@/lib/interests";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestTimelineChart", () => {
  it("renders the title and a legend entry for each series", () => {
    // Arrange
    const buckets: InterestTimelineBucket[] = [
      {
        week_start: "2026-07-27T00:00:00Z",
        interest_article_count: 4,
        topics: [
          { topic: "llm", positive_count: 2, negative_count: 1 },
          { topic: "rust", positive_count: 1, negative_count: 0 },
        ],
      },
    ];

    // Act
    render(<InterestTimelineChart buckets={buckets} />);

    // Assert
    expect(screen.getByRole("heading", { name: "関心の時間変化" })).toBeInTheDocument();
    expect(screen.getByText("関心記事の追加件数")).toBeInTheDocument();
    expect(screen.getByText("Good・保存")).toBeInTheDocument();
    expect(screen.getByText("Bad")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when there are no buckets", () => {
    // Arrange & Act
    render(<InterestTimelineChart buckets={[]} />);

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
