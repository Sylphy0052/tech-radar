import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestOriginChart } from "@/components/features/InterestOriginChart";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestOriginChart", () => {
  it("renders the title, the population note, and a name-with-count label for each non-zero origin", () => {
    // Arrange & Act
    render(
      <InterestOriginChart
        originCounts={{
          manual_count: 3,
          good_count: 5,
          saved_count: 2,
          read_full_count: 0,
          clicked_count: 1,
        }}
      />,
    );

    // Assert
    expect(screen.getByRole("heading", { name: "関心プロファイルへの寄与元" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "関心プロファイルの構築対象（手動登録・Good・保存・全文閲覧・クリック）を集計しています。",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("手動登録: 3")).toBeInTheDocument();
    expect(screen.getByText("Good: 5")).toBeInTheDocument();
    expect(screen.getByText("保存: 2")).toBeInTheDocument();
    expect(screen.getByText("クリック: 1")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("excludes an origin with a zero count from the slices", () => {
    // Arrange & Act
    render(
      <InterestOriginChart
        originCounts={{
          manual_count: 3,
          good_count: 0,
          saved_count: 0,
          read_full_count: 0,
          clicked_count: 0,
        }}
      />,
    );

    // Assert
    expect(screen.queryByText("Good: 0")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when all counts are zero", () => {
    // Arrange & Act
    render(
      <InterestOriginChart
        originCounts={{
          manual_count: 0,
          good_count: 0,
          saved_count: 0,
          read_full_count: 0,
          clicked_count: 0,
        }}
      />,
    );

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
