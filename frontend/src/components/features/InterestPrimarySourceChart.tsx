"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import { CHART_ANIMATION_ACTIVE, CHART_SERIES, renderPieSliceLabel } from "@/lib/chart-colors";
import type { ChartPieSlice } from "@/lib/chart-colors";
import { GOOD_OR_SAVED_ONLY_NOTE } from "@/lib/interests";
import type { InterestPrimarySourceRatio } from "@/lib/interests";

interface InterestPrimarySourceChartProps {
  primarySourceRatio: InterestPrimarySourceRatio;
}

function toSlices(ratio: InterestPrimarySourceRatio): ChartPieSlice[] {
  const candidates: ChartPieSlice[] = [
    { name: "公式・一次情報", value: ratio.primary_count, color: CHART_SERIES[0] },
    { name: "解説記事など", value: ratio.secondary_count, color: CHART_SERIES[2] },
  ];
  return candidates.filter((slice) => slice.value > 0);
}

/**
 * 公式情報と解説記事の比率（可視化 5/9、`summary.primary_source_ratio`）。
 *
 * `articles.is_primary_source` の内訳を円グラフで表す。凡例は使わず、各
 * スライスに `名称: 件数` を直接ラベルする（理由は `InterestFeedbackRatioChart`
 * と同じ）。
 */
export function InterestPrimarySourceChart({ primarySourceRatio }: InterestPrimarySourceChartProps) {
  const total = primarySourceRatio.primary_count + primarySourceRatio.secondary_count;
  const slices = toSlices(primarySourceRatio);

  return (
    <InterestChartCard
      title="公式情報と解説記事の比率"
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
