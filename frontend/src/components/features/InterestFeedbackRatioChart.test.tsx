import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestFeedbackRatioChart } from "@/components/features/InterestFeedbackRatioChart";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestFeedbackRatioChart", () => {
  it("renders the title and a name-with-count label for each non-zero action", () => {
    // Arrange & Act
    render(
      <InterestFeedbackRatioChart feedbackRatio={{ good_count: 5, bad_count: 2, save_count: 0 }} />,
    );

    // Assert
    expect(screen.getByRole("heading", { name: "Good/Bad比率" })).toBeInTheDocument();
    expect(screen.getByText("Good: 5")).toBeInTheDocument();
    expect(screen.getByText("Bad: 2")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("excludes an action with a zero count from the slices", () => {
    // Arrange & Act
    render(
      <InterestFeedbackRatioChart feedbackRatio={{ good_count: 5, bad_count: 0, save_count: 0 }} />,
    );

    // Assert
    expect(screen.queryByText("Bad: 0")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when all counts are zero", () => {
    // Arrange & Act
    render(
      <InterestFeedbackRatioChart feedbackRatio={{ good_count: 0, bad_count: 0, save_count: 0 }} />,
    );

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
