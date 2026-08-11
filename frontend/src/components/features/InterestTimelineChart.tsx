"use client";

import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import {
  CHART_ANIMATION_ACTIVE,
  CHART_AXIS,
  CHART_GRID,
  CHART_SERIES,
  CHART_STATUS,
  CHART_TICK_FILL,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
} from "@/lib/chart-colors";
import { formatWeekLabel } from "@/lib/interests";
import type { InterestTimelineBucket } from "@/lib/interests";

interface InterestTimelineChartProps {
  buckets: InterestTimelineBucket[];
}

interface TimelineRow {
  week: string;
  interestArticleCount: number;
  positiveCount: number;
  negativeCount: number;
}

/**
 * `topics` はトピック粒度の集計のため、週全体の positive/negative 件数を
 * 見るにはバケット内で合算する必要がある（バックエンドは意図的にトピック別の
 * まま返す。折れ線グラフではトピック別まで出すと線が多すぎて読めなくなるため、
 * ここで週合計へ落とす）。
 */
function toChartRows(buckets: InterestTimelineBucket[]): TimelineRow[] {
  return buckets.map((bucket) => ({
    week: formatWeekLabel(bucket.week_start),
    interestArticleCount: bucket.interest_article_count,
    positiveCount: bucket.topics.reduce((sum, topic) => sum + topic.positive_count, 0),
    negativeCount: bucket.topics.reduce((sum, topic) => sum + topic.negative_count, 0),
  }));
}

/**
 * 関心の時間変化（可視化 3/9、`timeline.buckets`）。
 *
 * 週ごとの関心記事追加件数（`user_articles` 由来）と、フィードバックの
 * positive/negative 件数（`article_feedback` 由来）を折れ線グラフで表す。
 * 単位が揃っている（いずれも「件数」）ため、二軸グラフにせず1つの軸へ載せる。
 */
export function InterestTimelineChart({ buckets }: InterestTimelineChartProps) {
  const rows = toChartRows(buckets);

  return (
    <InterestChartCard title="関心の時間変化" isEmpty={rows.length === 0}>
      <ResponsiveContainer width="100%" height={280}>
        <LineChart data={rows}>
          <CartesianGrid stroke={CHART_GRID} vertical={false} />
          <XAxis dataKey="week" stroke={CHART_AXIS} tick={{ fontSize: 12, fill: CHART_TICK_FILL }} />
          <YAxis stroke={CHART_AXIS} allowDecimals={false} tick={{ fill: CHART_TICK_FILL }} />
          <Tooltip contentStyle={CHART_TOOLTIP_CONTENT_STYLE} labelStyle={CHART_TOOLTIP_LABEL_STYLE} />
          <Legend />
          <Line
            type="monotone"
            dataKey="interestArticleCount"
            name="関心記事の追加件数"
            stroke={CHART_SERIES[0]}
            strokeWidth={2}
            dot={{ r: 3 }}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          />
          <Line
            type="monotone"
            dataKey="positiveCount"
            name="Good・保存"
            stroke={CHART_STATUS.good}
            strokeWidth={2}
            dot={{ r: 3 }}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          />
          <Line
            type="monotone"
            dataKey="negativeCount"
            name="Bad"
            stroke={CHART_STATUS.critical}
            strokeWidth={2}
            dot={{ r: 3 }}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          />
        </LineChart>
      </ResponsiveContainer>
    </InterestChartCard>
  );
}
