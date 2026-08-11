import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestClusterList } from "@/components/features/InterestClusterList";
import type { InterestClusterItem } from "@/lib/interests";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("InterestClusterList", () => {
  it("renders the title, cluster label, weight, and every topic in the cluster", () => {
    // Arrange
    const clusters: InterestClusterItem[] = [
      {
        label: "生成AI",
        weight: 0.82,
        topics: ["llm", "rag", "embedding"],
        updated_at: "2026-08-01T00:00:00Z",
      },
    ];

    // Act
    render(<InterestClusterList clusters={clusters} />);

    // Assert
    expect(screen.getByRole("heading", { name: "複数の関心クラスタ" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "生成AI" })).toBeInTheDocument();
    expect(screen.getByText("重み 0.82")).toBeInTheDocument();
    expect(screen.getByText("llm")).toBeInTheDocument();
    expect(screen.getByText("rag")).toBeInTheDocument();
    expect(screen.getByText("embedding")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an empty state when there are no clusters", () => {
    // Arrange & Act
    render(<InterestClusterList clusters={[]} />);

    // Assert
    expect(screen.getByText("関心クラスタはまだありません。")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
