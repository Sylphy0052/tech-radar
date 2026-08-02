import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestGenreChart } from "@/components/features/InterestGenreChart";
import type { InterestGenreItem } from "@/lib/interests";

describe("InterestGenreChart", () => {
  it("renders the title and a legend entry for each series", () => {
    // Arrange
    const genres: InterestGenreItem[] = [
      { domain: "qiita.com", positive_count: 3, negative_count: 1 },
      { domain: null, positive_count: 1, negative_count: 0 },
    ];

    // Act
    render(<InterestGenreChart genres={genres} />);

    // Assert
    expect(screen.getByRole("heading", { name: "ジャンル別関心度" })).toBeInTheDocument();
    expect(screen.getByText("Good・保存")).toBeInTheDocument();
    expect(screen.getByText("Bad")).toBeInTheDocument();
  });

  it("labels an unclassified domain as 未分類", () => {
    // Arrange
    const genres: InterestGenreItem[] = [{ domain: null, positive_count: 2, negative_count: 0 }];

    // Act
    render(<InterestGenreChart genres={genres} />);

    // Assert
    expect(screen.getByText("未分類")).toBeInTheDocument();
  });

  it("shows an empty state when there are no genres", () => {
    // Arrange & Act
    render(<InterestGenreChart genres={[]} />);

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
    expect(screen.queryByText("Good・保存")).not.toBeInTheDocument();
  });
});
