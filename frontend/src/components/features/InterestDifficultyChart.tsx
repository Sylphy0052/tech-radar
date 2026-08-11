"use client";

import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { InterestChartCard } from "@/components/features/InterestChartCard";
import {
  CHART_ANIMATION_ACTIVE,
  CHART_AXIS,
  CHART_GRID,
  CHART_MUTED,
  CHART_ORDINAL,
  CHART_TICK_FILL,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
} from "@/lib/chart-colors";
import { DIFFICULTY_LABELS, formatNullableLabel, GOOD_OR_SAVED_ONLY_NOTE } from "@/lib/interests";
import type { InterestDifficultyItem } from "@/lib/interests";

interface InterestDifficultyChartProps {
  difficulties: InterestDifficultyItem[];
}

/** 難易度は「初級→上級」という順序を持つため、単色の濃淡（ordinal ramp）で表す。 */
const DIFFICULTY_ORDER: Record<string, number> = {
  beginner: 0,
  intermediate: 1,
  advanced: 2,
};

function colorForDifficulty(difficulty: string | null): string {
  const orderIndex = difficulty === null ? undefined : DIFFICULTY_ORDER[difficulty];
  return orderIndex === undefined ? CHART_MUTED : CHART_ORDINAL[orderIndex];
}

interface DifficultyChartRow {
  difficulty: string;
  count: number;
  color: string;
}

/** 未分類（`DIFFICULTY_ORDER` に無い値）は既知の難易度より後ろへ置くための順位。 */
const UNKNOWN_DIFFICULTY_ORDER = Object.keys(DIFFICULTY_ORDER).length;

function orderOf(difficulty: string | null): number {
  const orderIndex = difficulty === null ? undefined : DIFFICULTY_ORDER[difficulty];
  return orderIndex ?? UNKNOWN_DIFFICULTY_ORDER;
}

/**
 * API は件数降順で返すが、難易度は「初級→中級→上級」という順序を持つ項目のため
 * 表示は必ずその順（未分類は末尾）へ並べ替える。件数順のままだと単色ランプの
 * 濃淡が並び順と食い違い、濃い棒＝上級という手掛かりが読み取れなくなる。
 */
function toChartRows(items: InterestDifficultyItem[]): DifficultyChartRow[] {
  return [...items]
    .sort((left, right) => orderOf(left.difficulty) - orderOf(right.difficulty))
    .map((item) => ({
      difficulty: formatNullableLabel(item.difficulty, DIFFICULTY_LABELS),
      count: item.count,
      color: colorForDifficulty(item.difficulty),
    }));
}

/**
 * 難易度の分布（可視化 7/9、`summary.difficulties`）。
 *
 * 難易度（`articles.difficulty`）別の件数を棒グラフで表す。単一系列だが、
 * 「初級→上級」という順序を持つ項目なのでカテゴリカルパレットではなく
 * 単色の濃淡ランプ（`CHART_ORDINAL`）で棒ごとに色分けする。
 */
export function InterestDifficultyChart({ difficulties }: InterestDifficultyChartProps) {
  const rows = toChartRows(difficulties);
  const total = rows.reduce((sum, row) => sum + row.count, 0);

  return (
    <InterestChartCard title="難易度の分布" description={GOOD_OR_SAVED_ONLY_NOTE} isEmpty={total === 0}>
      <ResponsiveContainer width="100%" height={280}>
        <BarChart data={rows}>
          <CartesianGrid stroke={CHART_GRID} vertical={false} />
          <XAxis dataKey="difficulty" stroke={CHART_AXIS} tick={{ fontSize: 12, fill: CHART_TICK_FILL }} />
          <YAxis stroke={CHART_AXIS} allowDecimals={false} tick={{ fill: CHART_TICK_FILL }} />
          <Tooltip contentStyle={CHART_TOOLTIP_CONTENT_STYLE} labelStyle={CHART_TOOLTIP_LABEL_STYLE} />
          <Bar dataKey="count" name="件数" radius={[4, 4, 0, 0]} isAnimationActive={CHART_ANIMATION_ACTIVE}>
            {rows.map((row) => (
              <Cell key={row.difficulty} fill={row.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </InterestChartCard>
  );
}
