import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ArticleCard } from "@/components/features/ArticleCard";
import type { FeedItem } from "@/lib/feed";
import { BAD_REASON_LABELS } from "@/lib/feedback";

function makeItem(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    article_id: "11111111-1111-1111-1111-111111111111",
    canonical_url: "https://example.com/a",
    feedback: null,
    is_primary_source: false,
    is_read: false,
    language: "en",
    original_url: "https://example.com/a?ref=1",
    published_at: "2026-08-01T00:00:00Z",
    rank: 1,
    reasons: {
      summary: "関心との一致度が高いため、上位に表示しています。",
      interest_similarity: 0.8,
    },
    score: 0.734,
    source_domain: "example.com",
    summary_ja: "日本語の要約です。",
    technologies: ["TypeScript"],
    title: "Original Title",
    topics: ["frontend"],
    translated_title: "日本語タイトル",
    ...overrides,
  };
}

function renderCard(overrides: Partial<FeedItem> = {}, onFeedback = vi.fn(), onRemoveFeedback = vi.fn()) {
  render(
    <ArticleCard
      item={makeItem(overrides)}
      onFeedback={onFeedback}
      onRemoveFeedback={onRemoveFeedback}
    />,
  );
  return { onFeedback, onRemoveFeedback };
}

describe("ArticleCard", () => {
  it("shows the primary source badge only when is_primary_source is true", () => {
    // Arrange / Act
    const { unmount } = render(
      <ArticleCard item={makeItem({ is_primary_source: false })} onFeedback={vi.fn()} onRemoveFeedback={vi.fn()} />,
    );

    // Assert
    expect(screen.queryByText("公式・一次情報")).not.toBeInTheDocument();
    unmount();

    render(
      <ArticleCard item={makeItem({ is_primary_source: true })} onFeedback={vi.fn()} onRemoveFeedback={vi.fn()} />,
    );
    expect(screen.getByText("公式・一次情報")).toBeInTheDocument();
  });

  it("shows a read marker for a read article", () => {
    // Arrange / Act
    renderCard({ is_read: true });

    // Assert
    expect(screen.getByText("既読")).toBeInTheDocument();
  });

  it("does not show the Japanese title row when translated_title is null", () => {
    // Arrange / Act
    renderCard({ translated_title: null });

    // Assert
    expect(screen.queryByText("日本語タイトル")).not.toBeInTheDocument();
  });

  it("calls onFeedback with good when the Good button is pressed", () => {
    // Arrange
    const { onFeedback } = renderCard();

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Good" }));

    // Assert
    expect(onFeedback).toHaveBeenCalledWith("good");
  });

  it("shows the Good button as pressed when the article already has good feedback", () => {
    // Arrange / Act
    renderCard({ feedback: { action: "good", reason: null, created_at: "2026-08-01T00:00:00Z" } });

    // Assert
    expect(screen.getByRole("button", { name: "Good" })).toHaveAttribute("aria-pressed", "true");
  });

  it("opens the reason picker when the Bad button is pressed", () => {
    // Arrange
    renderCard();

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Bad" }));

    // Assert
    expect(screen.getByLabelText(BAD_REASON_LABELS.too_shallow)).toBeInTheDocument();
  });

  it("calls onFeedback with bad and no reason when submitted without selecting one", () => {
    // Arrange
    const { onFeedback } = renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Bad" }));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "理由なしで送信" }));

    // Assert
    expect(onFeedback).toHaveBeenCalledWith("bad", undefined);
  });

  it("calls onFeedback with bad and the selected reason when submitted", () => {
    // Arrange
    const { onFeedback } = renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Bad" }));
    fireEvent.click(screen.getByLabelText(BAD_REASON_LABELS.too_shallow));

    // Act
    fireEvent.click(screen.getByRole("button", { name: "この理由で送信" }));

    // Assert
    expect(onFeedback).toHaveBeenCalledWith("bad", "too_shallow");
  });

  it("calls onFeedback with save when the save button is pressed", () => {
    // Arrange
    const { onFeedback } = renderCard();

    // Act
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    // Assert
    expect(onFeedback).toHaveBeenCalledWith("save");
  });

  it("calls onRemoveFeedback when the existing feedback is withdrawn", () => {
    // Arrange
    const { onRemoveFeedback } = renderCard({
      feedback: { action: "good", reason: null, created_at: "2026-08-01T00:00:00Z" },
    });

    // Act
    fireEvent.click(screen.getByRole("button", { name: "フィードバックを取り消す" }));

    // Assert
    expect(onRemoveFeedback).toHaveBeenCalled();
  });

  it("links to the original article in a new tab without leaking window.opener", () => {
    // Arrange / Act
    renderCard();

    // Assert
    const link = screen.getByRole("link", { name: "元記事を開く" });
    expect(link).toHaveAttribute("href", "https://example.com/a?ref=1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer noopener");
  });

  it("keeps the score breakdown collapsed by default and reveals it on demand", () => {
    // Arrange
    renderCard();

    // Assert (collapsed)
    expect(screen.getByText("関心との一致度が高いため、上位に表示しています。")).toBeInTheDocument();
    expect(screen.queryByText("0.800")).not.toBeInTheDocument();

    // Act
    fireEvent.click(screen.getByRole("button", { name: "スコア内訳を見る" }));

    // Assert (expanded)
    expect(screen.getByText("0.800")).toBeInTheDocument();
  });
});
