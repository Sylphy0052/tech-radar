import type { CSSProperties } from "react";
import type { PieLabelRenderProps } from "recharts";

/**
 * 関心分析画面（Issue #16）のグラフ配色を1箇所にまとめる。
 *
 * 実際の色値は `globals.css` の CSS カスタムプロパティ（`--chart-*`）側に持たせ、
 * ここでは recharts の `fill` / `stroke` へそのまま渡せる `var(--chart-*)` 文字列
 * だけを export する。画面はダーク固定（Issue #38）なので、コンポーネント側に
 * 明暗の判定ロジックを持たせず、色の変更は `globals.css` だけで完結させる。
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
 * 軸の目盛りラベル（数値・カテゴリ名）の文字色。
 *
 * 軸線・罫線（`CHART_AXIS`/`CHART_GRID`）は控えめな色に留める一方、実際に
 * 読む文字はそれより高いコントラストが要るため、本文の補助文字と同じ
 * `--ink-muted` を割り当てる（Issue #38、ダーク固定）。
 */
export const CHART_TICK_FILL = "var(--ink-muted)";

/**
 * recharts の Tooltip 既定スタイル（白背景+黒文字）はダーク面の上で浮いて
 * しまうため、パネルの手前面（`--surface-raised`）+ 操作可能な境界線
 * （`--line-strong`）+ 本文文字（`--ink`）に揃える（Issue #38）。
 *
 * `contentStyle` は枠全体、`labelStyle` はツールチップ先頭のカテゴリ名部分に
 * それぞれ渡す recharts 側の分割に合わせて2つに分けている。系列ごとの値
 * （`itemStyle`）は各系列の色（`entry.color`、`fill`/`stroke` に渡した
 * `CHART_SERIES` 等の値）を既定で使うため、ここでは上書きしない。
 */
export const CHART_TOOLTIP_CONTENT_STYLE: CSSProperties = {
  backgroundColor: "var(--surface-raised)",
  border: "1px solid var(--line-strong)",
  borderRadius: "var(--radius)",
  color: "var(--ink)",
};

export const CHART_TOOLTIP_LABEL_STYLE: CSSProperties = {
  color: "var(--ink)",
};

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

/**
 * 円グラフのスライス同士を区切る線。
 *
 * recharts の既定は白のため、ダーク面の上ではスライスの外周が光って見えてしまう。
 * カードの地色（`--surface`）を渡して、切れ目だけが見える状態にする（Issue #38）。
 */
export const CHART_PIE_STROKE = "var(--surface)";

/** 円グラフのスライス1件（名称・値・色）。4つの円グラフコンポーネントで共通の形。 */
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
 * 表示側でも防御的にフォールバックする。4つの円グラフコンポーネントで
 * 同じ組み立て方をするため1箇所にまとめる。
 */
export function renderPieSliceLabel({ name, value }: PieLabelRenderProps): string {
  return `${name ?? ""}: ${value ?? 0}`;
}
