import "@testing-library/jest-dom/vitest";

import { cloneElement, createElement, isValidElement } from "react";
import type { ReactNode } from "react";
import { afterEach, vi } from "vitest";

const MOCK_CHART_WIDTH = 600;
const MOCK_CHART_HEIGHT = 400;

/**
 * recharts の `ResponsiveContainer` は親要素のサイズを `ResizeObserver` で計測して
 * 子のグラフ（BarChart/LineChart/PieChart 等）へ `width`/`height` を渡すが、jsdom は
 * レイアウト計算をせず `ResizeObserver` も持たないため常に 0x0 になり、内部の
 * グラフ要素が一切描画されない（テストが空振りする）。単に固定サイズの `div` で
 * 包むだけでは子へ寸法が渡らず同じ問題が残るため、`ResponsiveContainer` の子
 * 要素へ直接 `width`/`height` を注入する形で差し替える（他の recharts
 * コンポーネントは実装のまま使う）。全テストファイル共通のため、個別のテストで
 * `vi.mock` を重複させずここへ集約する。
 *
 * 例外は `InterestAnalysisDashboard.test.tsx` で、7 種のグラフを一度に描画する
 * 重さを避けるため recharts 全体を素通しの `div` へ差し替える `vi.mock` を
 * ファイル内に持ち、この共通の差し替えを上書きする（Issue #41）。
 */
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("recharts")>();
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children, { width: MOCK_CHART_WIDTH, height: MOCK_CHART_HEIGHT } as object)
        : createElement("div", { style: { width: MOCK_CHART_WIDTH, height: MOCK_CHART_HEIGHT } }, children),
  };
});

/**
 * recharts は軸目盛等の文字幅を測るため、`#recharts_measurement_span` という
 * 隠しノードを `document.body` 直下（React コンポーネントツリーの外）へ1つだけ
 * 追加し、直近に測定した文字列を残したまま使い回す。React Testing Library の
 * 自動クリーンアップはレンダーコンテナだけを取り除くためこのノードは消えず、
 * 後続のテストで `screen.getByText` がこの残留ノードにもヒットして誤爆する。
 * 各テスト後に取り除き、テスト間で状態が漏れないようにする。
 */
afterEach(() => {
  document.getElementById("recharts_measurement_span")?.remove();
});
