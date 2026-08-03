import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestDifficultyChart } from "@/components/features/InterestDifficultyChart";
import type { InterestDifficultyItem } from "@/lib/interests";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestDifficultyChart", () => {
  it("renders the title, the good/save-only note, and a Japanese tick label per difficulty", () => {
    // Arrange
    const difficulties: InterestDifficultyItem[] = [
      { difficulty: "beginner", count: 5 },
      { difficulty: "advanced", count: 2 },
      { difficulty: null, count: 1 },
    ];

    // Act
    render(<InterestDifficultyChart difficulties={difficulties} />);

    // Assert
    expect(screen.getByRole("heading", { name: "難易度の分布" })).toBeInTheDocument();
    expect(screen.getByText("Good・保存した記事のみを対象にしています。")).toBeInTheDocument();
    expect(screen.getByText("初級")).toBeInTheDocument();
    expect(screen.getByText("上級")).toBeInTheDocument();
    expect(screen.getByText("未分類")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("orders ticks from beginner to advanced with unclassified last, regardless of count order", () => {
    // Arrange: API は件数降順で返す（上級 → 初級 → 未分類 → 中級）
    const difficulties: InterestDifficultyItem[] = [
      { difficulty: "advanced", count: 9 },
      { difficulty: "beginner", count: 5 },
      { difficulty: null, count: 3 },
      { difficulty: "intermediate", count: 1 },
    ];

    // Act
    render(<InterestDifficultyChart difficulties={difficulties} />);

    // Assert: 目盛ラベルの DOM 上の並びが難易度の順序（未分類は末尾）になる
    const card = screen.getByRole("heading", { name: "難易度の分布" }).closest("section");
    const expectedLabels = ["初級", "中級", "上級", "未分類"];
    const renderedLabels = Array.from(card?.querySelectorAll("tspan") ?? [])
      .map((node) => node.textContent)
      .filter((text): text is string => text !== null && expectedLabels.includes(text));
    expect(renderedLabels).toEqual(expectedLabels);
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when there are no articles counted", () => {
    // Arrange & Act
    render(<InterestDifficultyChart difficulties={[]} />);

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
