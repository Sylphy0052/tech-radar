"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import { CHART_ANIMATION_ACTIVE, CHART_SERIES, renderPieSliceLabel } from "@/lib/chart-colors";
import type { ChartPieSlice } from "@/lib/chart-colors";
import { CONTENT_TYPE_LABELS, formatNullableLabel, GOOD_OR_SAVED_ONLY_NOTE } from "@/lib/interests";
import type { InterestContentTypeItem } from "@/lib/interests";

interface InterestContentTypeChartProps {
  contentTypes: InterestContentTypeItem[];
}

/**
 * `content_type` の語彙数は少数（`concept`/`implementation`/`research`/`news` +
 * 未分類）なため、カテゴリカルパレットの先頭から順に割り当てるだけで足りる。
 */
function toSlices(items: InterestContentTypeItem[]): ChartPieSlice[] {
  return items
    .map((item, index) => ({
      name: formatNullableLabel(item.content_type, CONTENT_TYPE_LABELS),
      value: item.count,
      color: CHART_SERIES[index % CHART_SERIES.length],
    }))
    .filter((slice) => slice.value > 0);
}

/**
 * 概念/実装/研究/ニュースの比率（可視化 6/9、`summary.content_types`）。
 *
 * 記事の性質（`articles.content_type`）別の件数を円グラフで表す。凡例は使わず、
 * 各スライスに `名称: 件数` を直接ラベルする（理由は `InterestFeedbackRatioChart`
 * と同じ）。
 */
export function InterestContentTypeChart({ contentTypes }: InterestContentTypeChartProps) {
  const total = contentTypes.reduce((sum, item) => sum + item.count, 0);
  const slices = toSlices(contentTypes);

  return (
    <InterestChartCard
      title="概念・実装・研究・ニュースの比率"
      description={GOOD_OR_SAVED_ONLY_NOTE}
      isEmpty={total === 0}
    >
      <ResponsiveContainer width="100%" height={280}>
        <PieChart>
          <Pie
            data={slices}
            dataKey="value"
            nameKey="name"
            label={renderPieSliceLabel}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          >
            {slices.map((slice) => (
              <Cell key={slice.name} fill={slice.color} />
            ))}
          </Pie>
          <Tooltip />
        </PieChart>
      </ResponsiveContainer>
    </InterestChartCard>
  );
}
