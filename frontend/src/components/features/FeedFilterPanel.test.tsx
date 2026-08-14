import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FeedFilterPanel } from "@/components/features/FeedFilterPanel";
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
      <FeedFilterPanel />
    </NavigationTestProvider>,
  );
}

/** テスト対象コンポーネントの直下から現在の URL クエリを覗くための小道具。 */
function LocationProbe() {
  const { searchParams } = useNavigationTestContext();
  return <output data-testid="location-probe">{searchParams.toString()}</output>;
}

describe("FeedFilterPanel", () => {
  it("restores field values from the current URL on mount", () => {
    // Arrange & Act
    renderPanel("q=rust&topics=ai&topics=web&source_domain=blog.example.com");

    // Assert
    expect(screen.getByLabelText("検索語")).toHaveValue("rust");
    expect(screen.getByLabelText("トピック（カンマ区切りで複数指定可）")).toHaveValue("ai, web");
    expect(screen.getByLabelText("情報源")).toHaveValue("blog.example.com");
  }, TEST_TIMEOUT_MS);

  it("reflects the search term in the URL query when submitted alone", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("検索語"), { target: { value: "rust" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("q")).toBe("rust");
    expect(query.getAll("topics")).toEqual([]);
  }, TEST_TIMEOUT_MS);

  it("splits the comma-separated topics/technologies fields into multiple query values", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("トピック（カンマ区切りで複数指定可）"), {
      target: { value: "ai, web ,  " },
    });
    fireEvent.change(screen.getByLabelText("技術タグ（カンマ区切りで複数指定可）"), {
      target: { value: "rust,wasm" },
    });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert — 空白のみの要素・末尾のカンマは捨てる
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.getAll("topics")).toEqual(["ai", "web"]);
    expect(query.getAll("technologies")).toEqual(["rust", "wasm"]);
  }, TEST_TIMEOUT_MS);

  it("reflects the source domain in the URL query when submitted", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("情報源"), { target: { value: "blog.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("source_domain")).toBe("blog.example.com");
  }, TEST_TIMEOUT_MS);

  it("converts the published date range fields to JST-day boundaries in UTC", () => {
    // Arrange
    render(
      <NavigationTestProvider>
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("公開日（開始）"), { target: { value: "2026-08-01" } });
    fireEvent.change(screen.getByLabelText("公開日（終了）"), { target: { value: "2026-08-01" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("published_from")).toBe("2026-07-31T15:00:00.000Z");
    expect(query.get("published_to")).toBe("2026-08-01T14:59:59.999Z");
  }, TEST_TIMEOUT_MS);

  it("reflects the feed window in the URL query when submitted", () => {
    // Arrange — 受入基準: 対象期間が URL クエリに載る（Issue #90 自己レビュー）
    render(
      <NavigationTestProvider>
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("対象期間（1〜180日）"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("max_age_days")).toBe("30");
  }, TEST_TIMEOUT_MS);

  it("drops an out-of-range feed window instead of putting it in the URL", () => {
    // Arrange — number 入力は min/max を付けても範囲外の値を送れることがあるため、
    // 送信時にも確かめて「指定なし」（backend の既定）へ落とす
    render(
      <NavigationTestProvider>
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.change(screen.getByLabelText("対象期間（1〜180日）"), { target: { value: "999" } });
    fireEvent.click(screen.getByRole("button", { name: "絞り込む" }));

    // Assert
    const query = new URLSearchParams(screen.getByTestId("location-probe").textContent ?? "");
    expect(query.get("max_age_days")).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("restores the feed window from the current URL on mount", () => {
    // Arrange & Act
    renderPanel("max_age_days=45");

    // Assert
    expect(screen.getByLabelText("対象期間（1〜180日）")).toHaveValue(45);
  }, TEST_TIMEOUT_MS);

  it("clears the URL query when the clear button is pressed", () => {
    // Arrange
    render(
      <NavigationTestProvider initialSearch="q=rust&source_domain=blog.example.com">
        <FeedFilterPanel />
        <LocationProbe />
      </NavigationTestProvider>,
    );

    // Act
    fireEvent.click(screen.getByRole("button", { name: "クリア" }));

    // Assert
    expect(screen.getByTestId("location-probe")).toHaveTextContent("");
  }, TEST_TIMEOUT_MS);
});
