"use client";

import { useState } from "react";

import { BadReasonPicker } from "@/components/features/BadReasonPicker";
import { ScoreBreakdown } from "@/components/features/ScoreBreakdown";
import { formatDateTimeJa } from "@/lib/format-date";
import type { FeedItem } from "@/lib/feed";
import type { BadReason, FeedbackAction } from "@/lib/feedback";
import { isSafeHttpUrl } from "@/lib/safe-url";

interface ArticleCardProps {
  item: FeedItem;
  /** Good / Bad / 保存を記録する。`useFeed` の `applyFeedback` へそのまま渡す想定。 */
  onFeedback: (action: FeedbackAction, reason?: BadReason) => void;
  /** フィードバックを取り消す。`useFeed` の `removeFeedback` へそのまま渡す想定。 */
  onRemoveFeedback: () => void;
}

/** トピック・技術タグの一覧表示。空配列なら何も出さない。 */
function TagList({ label, tags }: { label: string; tags: readonly string[] }) {
  if (tags.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mono-label">{label}:</span>
      {tags.map((tag) => (
        <span key={tag} className="chip">
          {tag}
        </span>
      ))}
    </div>
  );
}

/**
 * Discover フィードの記事カード（`PROJECT_SPEC.md` §6.1）。
 *
 * Bad ボタンは押した瞬間に確定させず、理由選択（`BadReasonPicker`）を挟む。
 * 理由なしでも Bad は成立するため、キャンセルとは別に「理由なしで送信」を用意する。
 */
export function ArticleCard({ item, onFeedback, onRemoveFeedback }: ArticleCardProps) {
  const [isBadPickerOpen, setIsBadPickerOpen] = useState(false);

  function handleBadSubmit(reason?: BadReason): void {
    onFeedback("bad", reason);
    setIsBadPickerOpen(false);
  }

  return (
    <article className="panel panel-interactive flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 font-mono text-xs text-ink-subtle">
        {item.is_primary_source && <span className="chip-accent">公式・一次情報</span>}
        {item.is_read && <span>既読</span>}
        <span>{item.source_domain}</span>
        {item.published_at !== null && <span>{formatDateTimeJa(item.published_at)}</span>}
        {item.language !== null && <span>原文言語: {item.language}</span>}
      </div>

      <h3 className="text-lg font-semibold text-ink">{item.title}</h3>
      {item.translated_title !== null && (
        <p className="text-sm text-ink-muted">{item.translated_title}</p>
      )}
      {item.summary_ja !== null && <p className="text-sm text-ink">{item.summary_ja}</p>}

      <TagList label="トピック" tags={item.topics} />
      <TagList label="技術" tags={item.technologies} />

      <p className="mono-label">
        SCORE <span className="text-accent">{item.score.toFixed(3)}</span>
      </p>
      <ScoreBreakdown reasons={item.reasons} />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-pressed={item.feedback?.action === "good"}
          onClick={() => onFeedback("good")}
          className="btn"
        >
          Good
        </button>
        <button
          type="button"
          aria-pressed={item.feedback?.action === "bad"}
          onClick={() => setIsBadPickerOpen(true)}
          className="btn"
        >
          Bad
        </button>
        <button
          type="button"
          aria-pressed={item.feedback?.action === "save"}
          onClick={() => onFeedback("save")}
          className="btn"
        >
          保存
        </button>
        {item.feedback !== null && (
          <button type="button" onClick={onRemoveFeedback} className="link-inline text-xs">
            フィードバックを取り消す
          </button>
        )}
        {isSafeHttpUrl(item.original_url) ? (
          <a
            href={item.original_url}
            target="_blank"
            rel="noreferrer noopener"
            className="link-inline ml-auto text-sm"
          >
            元記事を開く
          </a>
        ) : (
          // http/https 以外のスキームはバックエンド側の検証をすり抜けない想定だが、
          // 万一届いてもリンク化せずテキスト表示に留める（多層防御）。
          <span className="ml-auto text-sm text-ink-subtle">元記事のリンクを表示できません</span>
        )}
      </div>

      {isBadPickerOpen && (
        <BadReasonPicker onSubmit={handleBadSubmit} onCancel={() => setIsBadPickerOpen(false)} />
      )}
    </article>
  );
}
