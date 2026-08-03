"use client";

import { useId, useState } from "react";

import type { FeedItem } from "@/lib/feed";

interface ScoreBreakdownProps {
  reasons: FeedItem["reasons"];
}

/** 機械生成の文章。他の項目とは扱いを分けて見出し扱いにする（`ranking.py` の `to_reasons`）。 */
const SUMMARY_KEY = "summary";

/**
 * 内訳項目キー（`backend/src/techradar/recommendation/ranking.py` の
 * `ScoreBreakdown.to_reasons`）の日本語ラベル対応表。未知のキーが来た場合は
 * 呼び出し側でキーをそのまま表示するため、ここには網羅している項目のみを持つ。
 */
const REASON_FIELD_LABELS: Record<string, string> = {
  interest_similarity: "関心一致度",
  source_authority: "情報源の権威性",
  source_article_match: "情報源と主題の一致度",
  freshness: "新しさ",
  technical_quality: "技術的な質",
  novelty: "新規性",
  authority_gate_factor: "権威性ゲート係数",
  interest_similarity_contribution: "関心一致度の寄与",
  source_authority_contribution: "情報源の権威性の寄与",
  source_article_match_contribution: "情報源と主題の一致度の寄与",
  freshness_contribution: "新しさの寄与",
  technical_quality_contribution: "技術的な質の寄与",
  novelty_contribution: "新規性の寄与",
  bad_penalty: "Bad済みの減点",
  bad_similarity_penalty: "Bad記事との近さの減点",
  duplicate_penalty: "重複の減点",
  read_penalty: "既読の減点",
  source_preference_factor: "情報源の選好係数",
  total: "合計スコア",
};

function formatReasonValue(value: number | string): string {
  return typeof value === "number" ? value.toFixed(3) : value;
}

/**
 * 推薦理由の内訳表示。`summary`（機械生成の1文）は常に見出しとして表示し、
 * それ以外の数値項目は既定で折りたたんだ内訳として表示する
 * （受入基準「スコア内訳がUIから確認できる」）。
 */
export function ScoreBreakdown({ reasons }: ScoreBreakdownProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const listId = useId();

  const summary = reasons[SUMMARY_KEY];
  const entries = Object.entries(reasons).filter(([key]) => key !== SUMMARY_KEY);

  return (
    <div className="flex flex-col gap-1">
      {typeof summary === "string" && <p className="text-sm">{summary}</p>}
      <button
        type="button"
        aria-expanded={isExpanded}
        aria-controls={listId}
        onClick={() => setIsExpanded((current) => !current)}
        className="self-start font-mono text-xs tracking-wide text-ink-subtle underline underline-offset-2 hover:text-accent-strong"
      >
        {isExpanded ? "スコア内訳を閉じる" : "スコア内訳を見る"}
      </button>
      {isExpanded && (
        <dl id={listId} className="flex flex-col gap-1.5">
          {entries.map(([key, value]) => {
            const isNumeric = typeof value === "number";
            const barWidth = isNumeric ? `${Math.min(Math.abs(value), 1) * 100}%` : undefined;
            return (
              <div key={key} className="flex items-center gap-2">
                {/* 対応表に無いキーはそのまま表示するため、長い英字が来てもバーに
                    重ならないよう幅を固定したうえで省略する。 */}
                <dt className="mono-label w-40 shrink-0 truncate" title={key}>
                  {REASON_FIELD_LABELS[key] ?? key}
                </dt>
                {isNumeric && (
                  <div className="h-1 flex-1 bg-surface-raised">
                    <div className="h-full bg-accent" style={{ width: barWidth }} />
                  </div>
                )}
                <dd
                  className={`shrink-0 font-mono text-xs ${isNumeric ? "text-accent" : "text-ink"}`}
                >
                  {formatReasonValue(value)}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </div>
  );
}
