"use client";

import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import {
  CHART_ANIMATION_ACTIVE,
  CHART_PIE_STROKE,
  CHART_SERIES,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  renderPieSliceLabel,
} from "@/lib/chart-colors";
import type { ChartPieSlice } from "@/lib/chart-colors";
import { originLabel } from "@/lib/interest-articles";
import { PROFILE_POPULATION_NOTE } from "@/lib/interests";
import type { InterestOriginCounts } from "@/lib/interests";

interface InterestOriginChartProps {
  originCounts: InterestOriginCounts;
}

/** 件数0のスライスは円グラフに載せても意味が無いため描画対象から除く。 */
function toSlices(originCounts: InterestOriginCounts): ChartPieSlice[] {
  const candidates: ChartPieSlice[] = [
    { name: originLabel("manual"), value: originCounts.manual_count, color: CHART_SERIES[0] },
    { name: originLabel("good"), value: originCounts.good_count, color: CHART_SERIES[1] },
    { name: originLabel("saved"), value: originCounts.saved_count, color: CHART_SERIES[2] },
    {
      name: originLabel("read_full"),
      value: originCounts.read_full_count,
      color: CHART_SERIES[3],
    },
    { name: originLabel("clicked"), value: originCounts.clicked_count, color: CHART_SERIES[4] },
  ];
  return candidates.filter((slice) => slice.value > 0);
}

/**
 * 関心プロファイルへの寄与元（`summary.origin_counts`、Issue #92）。
 *
 * 集計対象がどの登録経路（手動登録/Good/保存/全文閲覧/クリック）から何件
 * 来ているかを円グラフで表す。他の可視化（技術・公式情報比率等）は
 * `article_feedback` の good/save に絞った集計だが、これは
 * `interest/service.py` の関心プロファイル構築対象（`user_articles` の
 * 5経路 + `article_feedback` の good のみの取りこぼし補完）を数えた別の
 * 母集団のため、専用の注記（`PROFILE_POPULATION_NOTE`）を出して混同を防ぐ。
 * 凡例は使わず、各スライスに `名称: 件数` を直接ラベルする
 * （`InterestFeedbackRatioChart` と同じ理由）。
 */
export function InterestOriginChart({ originCounts }: InterestOriginChartProps) {
  const total =
    originCounts.manual_count +
    originCounts.good_count +
    originCounts.saved_count +
    originCounts.read_full_count +
    originCounts.clicked_count;
  const slices = toSlices(originCounts);

  return (
    <InterestChartCard
      title="関心プロファイルへの寄与元"
      description={PROFILE_POPULATION_NOTE}
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
