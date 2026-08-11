"use client";

import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import {
  CHART_ANIMATION_ACTIVE,
  CHART_AXIS,
  CHART_GRID,
  CHART_STATUS,
  CHART_TICK_FILL,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
} from "@/lib/chart-colors";
import { formatNullableLabel } from "@/lib/interests";
import type { InterestGenreItem } from "@/lib/interests";

interface InterestGenreChartProps {
  genres: InterestGenreItem[];
}

/** 項目数に応じて縦幅を伸ばす（`InterestTechnologyChart` と同じ理由・同じ値）。 */
const ROW_HEIGHT_PX = 48;
const MIN_CHART_HEIGHT_PX = 240;

interface GenreChartRow {
  domain: string;
  positiveCount: number;
  negativeCount: number;
}

function toChartRows(genres: InterestGenreItem[]): GenreChartRow[] {
  return genres.map((genre) => ({
    domain: formatNullableLabel(genre.domain),
    positiveCount: genre.positive_count,
    negativeCount: genre.negative_count,
  }));
}

/**
 * ジャンル別関心度（可視化 1/9、`summary.genres`）。
 *
 * ジャンル（`articles.domain`）ごとに Good/保存（`positive_count`）と
 * Bad（`negative_count`）を並べた棒グラフで表す。Good/Bad は状態を表す固定色
 * （`CHART_STATUS`）を使い、識別用のカテゴリカルパレットとは分けて扱う。
 *
 * 縦棒ではなく横棒（`layout="vertical"`）にするのは、ジャンル名が
 * "Generative AI" / "Web Frontend" のように長く、縦棒の X 軸では recharts が
 * ラベルを自動で間引いてしまい、どのジャンルの棒か読めなくなるため
 * （`InterestTechnologyChart` と同じ理由）。
 */
export function InterestGenreChart({ genres }: InterestGenreChartProps) {
  const rows = toChartRows(genres);
  const height = Math.max(MIN_CHART_HEIGHT_PX, rows.length * ROW_HEIGHT_PX);

  return (
    <InterestChartCard title="ジャンル別関心度" isEmpty={rows.length === 0}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={rows} layout="vertical" margin={{ left: 16, right: 16 }}>
          <CartesianGrid stroke={CHART_GRID} horizontal={false} />
          <XAxis type="number" stroke={CHART_AXIS} allowDecimals={false} tick={{ fill: CHART_TICK_FILL }} />
          <YAxis
            type="category"
            dataKey="domain"
            stroke={CHART_AXIS}
            width={120}
            tick={{ fontSize: 12, fill: CHART_TICK_FILL }}
          />
          <Tooltip contentStyle={CHART_TOOLTIP_CONTENT_STYLE} labelStyle={CHART_TOOLTIP_LABEL_STYLE} />
          <Legend />
          <Bar
            dataKey="positiveCount"
            name="Good・保存"
            fill={CHART_STATUS.good}
            radius={[0, 4, 4, 0]}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          />
          <Bar
            dataKey="negativeCount"
            name="Bad"
            fill={CHART_STATUS.critical}
            radius={[0, 4, 4, 0]}
            isAnimationActive={CHART_ANIMATION_ACTIVE}
          />
        </BarChart>
      </ResponsiveContainer>
    </InterestChartCard>
  );
}
