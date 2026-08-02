import type { PieLabelRenderProps } from "recharts";

/**
 * 関心分析画面（Issue #16）のグラフ配色を1箇所にまとめる。
 *
 * 実際の色値は `globals.css` の CSS カスタムプロパティ（`--chart-*`）側に持たせ、
 * ここでは recharts の `fill` / `stroke` へそのまま渡せる `var(--chart-*)` 文字列
 * だけを export する。ライト/ダークの切替は CSS の `@media (prefers-color-scheme)`
 * が担うため、コンポーネント側でモード判定のロジックを持つ必要がない。
 */

/**
 * 識別（identity）用の固定順カテゴリカルパレット。8スロットまで。
 * 順序を変えない（隣接スロット間の色覚多様性対応の検証は順序込みで行っているため）。
 */
export const CHART_SERIES = [
  "var(--chart-series-1)",
  "var(--chart-series-2)",
  "var(--chart-series-3)",
  "var(--chart-series-4)",
  "var(--chart-series-5)",
  "var(--chart-series-6)",
  "var(--chart-series-7)",
  "var(--chart-series-8)",
] as const;

/** Good/Bad のような状態を表す固定色。カテゴリカルパレットとは独立に扱う。 */
export const CHART_STATUS = {
  good: "var(--chart-status-good)",
  critical: "var(--chart-status-critical)",
} as const;

/** 難易度（初級→上級）のような順序付きカテゴリ用の単色ランプ（薄→濃）。 */
export const CHART_ORDINAL = [
  "var(--chart-ordinal-1)",
  "var(--chart-ordinal-2)",
  "var(--chart-ordinal-3)",
] as const;

/** 未分類（null）を表す無彩色。カテゴリカル配色と混同しないよう別枠にする。 */
export const CHART_MUTED = "var(--chart-muted)";

/** グラフの罫線・軸線。データそのものより控えめな色にする。 */
export const CHART_GRID = "var(--chart-grid)";
export const CHART_AXIS = "var(--chart-axis)";

/**
 * recharts の初期表示アニメーション（`isAnimationActive`）を無効化する共通値。
 *
 * 理由は2つ: (1) フィルター操作の無いこの画面では、開くたびにアニメーションが
 * 走るより即座に値が読める方が有用。(2) jsdom は `requestAnimationFrame` を
 * 実ブラウザのようには進めないため、アニメーション開始直後の中間状態（Pie が
 * 半径0のまま等）で DOM が固定され、テストが空振りする（`vitest.setup.ts` の
 * `ResponsiveContainer` サイズ対策だけでは解決しない別要因のため、ここで
 * 分けて明示する）。
 */
export const CHART_ANIMATION_ACTIVE = false;

/** 円グラフのスライス1件（名称・値・色）。3つの円グラフコンポーネントで共通の形。 */
export interface ChartPieSlice {
  name: string;
  value: number;
  color: string;
}

/**
 * 円グラフのスライスに `名称: 件数` を直接ラベルする共通の `label` 関数。
 *
 * recharts の `Pie` の `label` prop は `PieLabelRenderProps` を受け取り、
 * `name`/`value` は型上 `undefined` を許容する（実行時には必ず渡ってくるが、
 * `Cell`/`data` の組み方次第では欠落しうるため型は緩めてある）ため、
 * 表示側でも防御的にフォールバックする。3つの円グラフコンポーネントで
 * 同じ組み立て方をするため1箇所にまとめる。
 */
export function renderPieSliceLabel({ name, value }: PieLabelRenderProps): string {
  return `${name ?? ""}: ${value ?? 0}`;
}
