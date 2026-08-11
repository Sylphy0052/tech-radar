import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestContentTypeChart } from "@/components/features/InterestContentTypeChart";
import type { InterestContentTypeItem } from "@/lib/interests";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestContentTypeChart", () => {
  it("renders the title and a Japanese name-with-count label for each content type", () => {
    // Arrange
    const contentTypes: InterestContentTypeItem[] = [
      { content_type: "concept", count: 4 },
      { content_type: "implementation", count: 2 },
      { content_type: null, count: 1 },
    ];

    // Act
    render(<InterestContentTypeChart contentTypes={contentTypes} />);

    // Assert
    expect(screen.getByRole("heading", { name: "概念・実装・研究・ニュースの比率" })).toBeInTheDocument();
    expect(screen.getByText("概念解説: 4")).toBeInTheDocument();
    expect(screen.getByText("実装・手順: 2")).toBeInTheDocument();
    expect(screen.getByText("未分類: 1")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when there are no articles counted", () => {
    // Arrange & Act
    render(<InterestContentTypeChart contentTypes={[]} />);

    // Assert
    expect(screen.getByText("まだデータがありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
