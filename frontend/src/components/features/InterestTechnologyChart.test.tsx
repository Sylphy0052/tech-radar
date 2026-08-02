import { render, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestTechnologyChart } from "@/components/features/InterestTechnologyChart";
import type { InterestTechnologyItem } from "@/lib/interests";

describe("InterestTechnologyChart", () => {
  it("renders the title and a tick label for each technology", () => {
    // Arrange
    const technologies: InterestTechnologyItem[] = [
      { technology: "Rust", count: 5 },
      { technology: "TypeScript", count: 3 },
    ];

    // Act
    // recharts はテキスト幅測定用の隠しノード（#recharts_measurement_span）を
    // `document.body` 直下（RTL のレンダーコンテナの外）へ追加し、直近に測定した
    // 文字列を残したままにする。`screen`（= document.body 全体を検索）だと
    // そのノードと実際の目盛ラベルの両方にヒットして複数要素エラーになるため、
    // レンダーコンテナ内だけを検索する `within(container)` を使う。
    const { container } = render(<InterestTechnologyChart technologies={technologies} />);
    const view = within(container);

    // Assert
    expect(view.getByRole("heading", { name: "よく読む企業・OSS・技術" })).toBeInTheDocument();
    expect(view.getByText("Good・保存した記事のみを対象にしています。")).toBeInTheDocument();
    expect(view.getByText("Rust")).toBeInTheDocument();
    expect(view.getByText("TypeScript")).toBeInTheDocument();
  });

  it("shows an empty state when there are no technologies", () => {
    // Arrange & Act
    const { container } = render(<InterestTechnologyChart technologies={[]} />);
    const view = within(container);

    // Assert
    expect(view.getByText("まだデータがありません。")).toBeInTheDocument();
    expect(view.queryByText("Rust")).not.toBeInTheDocument();
  });
});
