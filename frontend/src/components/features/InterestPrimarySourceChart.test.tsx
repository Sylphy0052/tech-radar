import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestPrimarySourceChart } from "@/components/features/InterestPrimarySourceChart";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestPrimarySourceChart", () => {
  it("renders the title, the good/save-only note, and a name-with-count label for each slice", () => {
    // Arrange & Act
    render(<InterestPrimarySourceChart primarySourceRatio={{ primary_count: 4, secondary_count: 6 }} />);

    // Assert
    expect(screen.getByRole("heading", { name: "公式情報と解説記事の比率" })).toBeInTheDocument();
    expect(screen.getByText("Good・保存した記事のみを対象にしています。")).toBeInTheDocument();
    expect(screen.getByText("公式・一次情報: 4")).toBeInTheDocument();
    expect(screen.getByText("解説記事など: 6")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("excludes a slice with a zero count", () => {
    // Arrange & Act
    render(<InterestPrimarySourceChart primarySourceRatio={{ primary_count: 4, secondary_count: 0 }} />);

    // Assert
    expect(screen.queryByText("解説記事など: 0")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when both counts are zero", () => {
    // Arrange & Act
    render(<InterestPrimarySourceChart primarySourceRatio={{ primary_count: 0, secondary_count: 0 }} />);

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
