"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import {
  CHART_ANIMATION_ACTIVE,
  CHART_PIE_STROKE,
  CHART_SERIES,
  CHART_STATUS,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  renderPieSliceLabel,
} from "@/lib/chart-colors";
import type { ChartPieSlice } from "@/lib/chart-colors";
import type { InterestFeedbackRatio } from "@/lib/interests";

interface InterestFeedbackRatioChartProps {
  feedbackRatio: InterestFeedbackRatio;
}

/** 件数0のスライスは円グラフに載せても意味が無いため描画対象から除く。 */
function toSlices(feedbackRatio: InterestFeedbackRatio): ChartPieSlice[] {
  const candidates: ChartPieSlice[] = [
    { name: "Good", value: feedbackRatio.good_count, color: CHART_STATUS.good },
    { name: "Bad", value: feedbackRatio.bad_count, color: CHART_STATUS.critical },
    { name: "保存", value: feedbackRatio.save_count, color: CHART_SERIES[0] },
  ];
  return candidates.filter((slice) => slice.value > 0);
}

/**
 * Good/Bad比率（可視化 2/9、`summary.feedback_ratio`）。
 *
 * action 別（good/bad/save）の件数を円グラフで表す。Good/Bad は状態を表す
 * 固定色（`CHART_STATUS`）、保存はそれ以外の識別としてカテゴリカルパレットの
 * 先頭色を使う。凡例（`<Legend>`）は使わず、各スライスに `名称: 件数` を直接
 * ラベルする。色だけに識別を頼らない（受入基準・dataviz skill 双方の要求）ために
 * 用意した凡例だが、円グラフ側からの自動連携が反映されず空のまま描画されて
 * しまうため、より確実な直接ラベルの方へ倒している。
 */
export function InterestFeedbackRatioChart({ feedbackRatio }: InterestFeedbackRatioChartProps) {
  const total = feedbackRatio.good_count + feedbackRatio.bad_count + feedbackRatio.save_count;
  const slices = toSlices(feedbackRatio);

  return (
    <InterestChartCard title="Good/Bad比率" isEmpty={total === 0}>
      <ResponsiveContainer width="100%" height={280}>
        <PieChart>
          <Pie
            data={slices}
            dataKey="value"
            nameKey="name"
            label={renderPieSliceLabel}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
            stroke={CHART_PIE_STROKE}
          >
            {slices.map((slice) => (
              <Cell key={slice.name} fill={slice.color} />
            ))}
          </Pie>
          <Tooltip contentStyle={CHART_TOOLTIP_CONTENT_STYLE} labelStyle={CHART_TOOLTIP_LABEL_STYLE} />
        </PieChart>
      </ResponsiveContainer>
    </InterestChartCard>
  );
}
