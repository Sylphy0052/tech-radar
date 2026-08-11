import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScoreBreakdown } from "@/components/features/ScoreBreakdown";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

const reasons = {
  interest_similarity: 0.8,
  source_authority: 0.5,
  total: 0.42,
  summary: "関心との一致度が高いため、上位に表示しています。",
};

describe("ScoreBreakdown", () => {
  it("shows the machine-generated summary sentence without expanding", () => {
    // Arrange / Act
    render(<ScoreBreakdown reasons={reasons} />);

    // Assert
    expect(
      screen.getByText("関心との一致度が高いため、上位に表示しています。"),
    ).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("keeps the numeric breakdown collapsed by default", () => {
    // Arrange / Act
    render(<ScoreBreakdown reasons={reasons} />);

    // Assert
    expect(screen.queryByText("0.800")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "スコア内訳を見る" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  }, TEST_TIMEOUT_MS);

  it("reveals the numeric breakdown with Japanese labels when expanded", () => {
    // Arrange
    render(<ScoreBreakdown reasons={reasons} />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "スコア内訳を見る" }));

    // Assert
    expect(screen.getByText("関心一致度")).toBeInTheDocument();
    expect(screen.getByText("0.800")).toBeInTheDocument();
    expect(screen.getByText("情報源の権威性")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("falls back to the raw key for an unknown reason field", () => {
    // Arrange
    render(<ScoreBreakdown reasons={{ some_future_field: 0.1, summary: "テスト理由。" }} />);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "スコア内訳を見る" }));

    // Assert
    expect(screen.getByText("some_future_field")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
