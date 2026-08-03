import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageShell } from "@/components/ui/PageShell";

describe("PageShell", () => {
  it("renders the given title as the level-1 heading", () => {
    // Arrange / Act
    render(
      <PageShell current="feed" title="TechRadar" description="説明文">
        <p>本文</p>
      </PageShell>,
    );

    // Assert
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("TechRadar");
  });

  it("renders the description and the children", () => {
    // Arrange / Act
    render(
      <PageShell current="feed" title="TechRadar" description="説明文">
        <p>本文</p>
      </PageShell>,
    );

    // Assert
    expect(screen.getByText("説明文")).toBeInTheDocument();
    expect(screen.getByText("本文")).toBeInTheDocument();
  });

  it("includes both the visible label and the Japanese description in the accessible name", () => {
    // Arrange / Act — 画面上は等幅の英字だけを見せつつ、日本語の説明も
    // アクセシブルネームに含める（WCAG 2.5.3 Label in Name）。
    render(
      <PageShell current="feed" title="TechRadar" description="説明文">
        <p>本文</p>
      </PageShell>,
    );

    // Assert
    expect(screen.getByRole("link", { name: "ARTICLES関心記事一覧を見る" })).toHaveAttribute(
      "href",
      "/articles",
    );
    expect(screen.getByRole("link", { name: "INTERESTS関心分析を見る" })).toHaveAttribute(
      "href",
      "/interests",
    );
    expect(screen.getByRole("link", { name: "FEEDフィードを見る" })).toHaveAttribute("href", "/");
  });

  it("marks only the current page link with aria-current", () => {
    // Arrange / Act
    render(
      <PageShell current="articles" title="関心記事一覧" description="説明文">
        <p>本文</p>
      </PageShell>,
    );

    // Assert
    expect(screen.getByRole("link", { name: /関心記事一覧を見る/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: /関心分析を見る/ })).not.toHaveAttribute(
      "aria-current",
    );
  });
});
