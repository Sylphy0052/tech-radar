import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InterestSuppressedTopicsList } from "@/components/features/InterestSuppressedTopicsList";
import type { SuppressedTopicItem } from "@/lib/interests";

describe("InterestSuppressedTopicsList", () => {
  it("renders the title and explicitly labels each topic as suppressed", () => {
    // Arrange
    const suppressedTopics: SuppressedTopicItem[] = [
      { topic: "広告", negative_weight: 0.8, effective_weight: -0.3 },
    ];

    // Act
    render(<InterestSuppressedTopicsList suppressedTopics={suppressedTopics} />);

    // Assert
    expect(screen.getByRole("heading", { name: "抑制中のジャンル" })).toBeInTheDocument();
    expect(screen.getByText("広告")).toBeInTheDocument();
    expect(screen.getByText("抑制中（抑制度 0.80）")).toBeInTheDocument();
  });

  it("shows an empty state when there are no suppressed topics", () => {
    // Arrange & Act
    render(<InterestSuppressedTopicsList suppressedTopics={[]} />);

    // Assert
    expect(screen.getByText("抑制中のトピックはありません。")).toBeInTheDocument();
  });
});
