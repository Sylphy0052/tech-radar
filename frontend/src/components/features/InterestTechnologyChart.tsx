"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import { CHART_ANIMATION_ACTIVE, CHART_AXIS, CHART_GRID, CHART_SERIES } from "@/lib/chart-colors";
import { GOOD_OR_SAVED_ONLY_NOTE } from "@/lib/interests";
import type { InterestTechnologyItem } from "@/lib/interests";

interface InterestTechnologyChartProps {
  technologies: InterestTechnologyItem[];
}

/** 項目数に応じて縦幅を伸ばす（項目が多いほどラベルが重なりやすいため）。 */
const ROW_HEIGHT_PX = 32;
const MIN_CHART_HEIGHT_PX = 240;

/**
 * よく読む企業・OSS・技術（可視化 4/9、`summary.technologies`）。
 *
 * 技術タグ（`articles.technologies`）別の関心記事件数を横棒グラフで表す。
 * 単一系列（件数のみ）のため、カテゴリカルパレットの先頭色1色で統一する
 * （凡例は単一系列では不要 — タイトルが系列名を兼ねる）。
 */
export function InterestTechnologyChart({ technologies }: InterestTechnologyChartProps) {
  const height = Math.max(MIN_CHART_HEIGHT_PX, technologies.length * ROW_HEIGHT_PX);

  return (
    <InterestChartCard
      title="よく読む企業・OSS・技術"
      description={GOOD_OR_SAVED_ONLY_NOTE}
      isEmpty={technologies.length === 0}
    >
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={technologies} layout="vertical" margin={{ left: 16, right: 16 }}>
          <CartesianGrid stroke={CHART_GRID} horizontal={false} />
          <XAxis type="number" stroke={CHART_AXIS} allowDecimals={false} />
          <YAxis type="category" dataKey="technology" stroke={CHART_AXIS} width={120} tick={{ fontSize: 12 }} />
          <Tooltip />
          <Bar
            dataKey="count"
            name="関心記事件数"
            fill={CHART_SERIES[0]}
            radius={[0, 4, 4, 0]}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          />
        </BarChart>
      </ResponsiveContainer>
    </InterestChartCard>
  );
}
