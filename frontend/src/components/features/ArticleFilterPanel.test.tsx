import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ArticleFilterPanel } from "@/components/features/ArticleFilterPanel";
import {
  NavigationTestProvider,
  useNavigationTestContext,
} from "@/test-utils/next-navigation-test-context";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

vi.mock("next/navigation", () => ({
  useSearchParams: () => useNavigationTestContext().searchParams,
  usePathname: () => useNavigationTestContext().pathname,
  useRouter: () => useNavigationTestContext().router,
}));

function renderPanel(initialSearch = ""): ReturnType<typeof render> {
  return render(
    <NavigationTestProvider initialSearch={initialSearch}>
      <ArticleFilterPanel />
    </NavigationTestProvider>,
  );
}

/** テスト対象コンポーネントの直下から現在の URL クエリを覗くための小道具。 */
function LocationProbe() {
  const { searchParams } = useNavigationTestContext();
  return <output data-testid="location-probe">{searchParams.toString()}</output>;
}

describe("ArticleFilterPanel", () => {
  it("restores checked origins and text values from the current URL on mount", () => {
    // Arrange & Act
    renderPanel("origin=good&origin=saved&domain=ai&language=ja");

    // Assert
    expect(screen.getByRole("checkbox", { name: "Good" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "保存" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "手動登録" })).not.toBeChecked();
    expect(screen.getByLabelText("ジャンル（大分類）")).toHaveValue("ai");
    expect(screen.getByLabelText("言語")).toHaveValue("ja");
  }, TEST_TIMEOUT_MS);

  it("restores the search query and the tag filters from the current URL on mount", () => {
    // Arrange & Act — トピック・技術タグは複数指定を受け付ける（Issue #91）
    renderPanel("q=Rust&topics=LLM&topics=RAG&technologies=Python");

    // Assert — 入力欄にはカンマ区切りで戻す
    expect(screen.getByLabelText("検索語")).toHaveValue("Rust");
    expect(screen.getByLabelText("トピック（カンマ区切りで複数指定可）")).toHaveValue("LLM, RAG");
    expect(screen.getByLabelText("技術タグ（カンマ区切りで複数指定可）")).toHaveValue("Python");
  }, TEST_TIMEOUT_MS);

  it("reflects the search query and the tag filters in the URL query when submitted", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <ArticleFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("検索語"), { target: { value: "Rust" } });
    fireEvent.change(screen.getByLabelText("トピック（カンマ区切りで複数指定可）"), {
      target: { value: "LLM, RAG" },
    });
    fireEvent.change(screen.getByLabelText("技術タグ（カンマ区切りで複数指定可）"), {
      target: { value: "Python" },
    });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("q")).toBe("Rust");
    expect(query.getAll("topics")).toEqual(["LLM", "RAG"]);
    expect(query.getAll("technologies")).toEqual(["Python"]);
  }, TEST_TIMEOUT_MS);

  it("drops empty entries from the comma separated tag fields", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <ArticleFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act — 連続カンマ・末尾カンマ・空白のみの要素を混ぜる
    fireEvent.change(screen.getByLabelText("トピック（カンマ区切りで複数指定可）"), {
      target: { value: " LLM ,, ,RAG, " },
    });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.getAll("topics")).toEqual(["LLM", "RAG"]);
  }, TEST_TIMEOUT_MS);

  it("reflects a single filter in the URL query when submitted alone", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <ArticleFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("ジャンル（大分類）"), { target: { value: "ai" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("domain")).toBe("ai");
    expect(query.getAll("origin")).toEqual([]);
  }, TEST_TIMEOUT_MS);

  it("reflects combined filters in the URL query when submitted together", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <ArticleFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.click(screen.getByRole("checkbox", { name: "Good" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "保存" }));
    fireEvent.change(screen.getByLabelText("情報源"), { target: { value: "blog.example.com" } });
    fireEvent.change(screen.getByLabelText("言語"), { target: { value: "ja" } });
    fireEvent.change(screen.getByLabelText("公式 / 非公式"), { target: { value: "true" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.getAll("origin")).toEqual(["good", "saved"]);
    expect(query.get("source_domain")).toBe("blog.example.com");
    expect(query.get("language")).toBe("ja");
    expect(query.get("is_primary_source")).toBe("true");
  }, TEST_TIMEOUT_MS);

  it("converts the date range fields to JST-day boundaries in UTC", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <ArticleFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("登録日時（開始）"), { target: { value: "2026-08-01" } });
    fireEvent.change(screen.getByLabelText("登録日時（終了）"), { target: { value: "2026-08-01" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("registered_from")).toBe("2026-07-31T15:00:00.000Z");
    expect(query.get("registered_to")).toBe("2026-08-01T14:59:59.999Z");
  }, TEST_TIMEOUT_MS);

  it("does not crash when registered_from in the URL is not a valid date", () => {
    // Arrange & Act — 不正な日付クエリ（共有リンク/手動編集で容易に到達しうる）
    renderPanel("registered_from=not-a-date");

    // Assert — レンダーが完了し、開始日フィールドは空欄にフォールバックする
    expect(screen.getByLabelText("登録日時（開始）")).toHaveValue("");
  }, TEST_TIMEOUT_MS);

  it("clears every filter from the URL when the clear button is pressed", () => {
    // Arrange
    render(
      <NavigationTestProvider initialSearch="origin=good&domain=ai">
        <ArticleFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.click(screen.getByRole("button", { name: "クリア" }));

    // Assert
    expect(screen.getByTestId("location-probe")).toHaveTextContent("");
  }, TEST_TIMEOUT_MS);
});
