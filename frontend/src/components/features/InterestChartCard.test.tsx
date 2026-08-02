import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestChartCard } from "@/components/features/InterestChartCard";

describe("InterestChartCard", () => {
  it("renders the title and children when not empty", () => {
    // Arrange & Act
    render(
      <InterestChartCard title="ジャンル別関心度" isEmpty={false}>
        <p>グラフ本体</p>
      </InterestChartCard>,
    );

    // Assert
    expect(screen.getByRole("heading", { name: "ジャンル別関心度" })).toBeInTheDocument();
    expect(screen.getByText("グラフ本体")).toBeInTheDocument();
  });

  it("renders the description when given", () => {
    // Arrange & Act
    render(
      <InterestChartCard title="難易度の分布" description="Good・保存した記事のみを対象" isEmpty={false}>
        <p>グラフ本体</p>
      </InterestChartCard>,
    );

    // Assert
    expect(screen.getByText("Good・保存した記事のみを対象")).toBeInTheDocument();
  });

  it("shows the default empty message and hides children when isEmpty is true", () => {
    // Arrange & Act
    render(
      <InterestChartCard title="ジャンル別関心度" isEmpty>
        <p>グラフ本体</p>
      </InterestChartCard>,
    );

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
    expect(screen.queryByText("グラフ本体")).not.toBeInTheDocument();
  });

  it("shows a custom empty message when given", () => {
    // Arrange & Act
    render(
      <InterestChartCard title="抑制中のジャンル" isEmpty emptyMessage="抑制中のトピックはありません。">
        <p>グラフ本体</p>
      </InterestChartCard>,
    );

    // Assert
    expect(screen.getByText("抑制中のトピックはありません。")).toBeInTheDocument();
  });
});
