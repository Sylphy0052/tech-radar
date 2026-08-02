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

const FEEDBACK_BUTTON_CLASS =
  "rounded border border-zinc-300 px-3 py-1 text-sm aria-pressed:bg-zinc-900 aria-pressed:text-white dark:border-zinc-700 dark:aria-pressed:bg-zinc-100 dark:aria-pressed:text-zinc-900";

/** トピック・技術タグの一覧表示。空配列なら何も出さない。 */
function TagList({ label, tags }: { label: string; tags: readonly string[] }) {
  if (tags.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs">
      <span className="text-zinc-500 dark:text-zinc-400">{label}:</span>
      {tags.map((tag) => (
        <span
          key={tag}
          className="rounded-full bg-zinc-100 px-2 py-0.5 dark:bg-zinc-800"
        >
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
    <article className="flex flex-col gap-2 rounded border border-zinc-200 p-4 dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
        {item.is_primary_source && (
          <span className="rounded-full bg-blue-100 px-2 py-0.5 text-blue-700 dark:bg-blue-950 dark:text-blue-300">
            公式・一次情報
          </span>
        )}
        {item.is_read && <span>既読</span>}
        <span>{item.source_domain}</span>
        {item.published_at !== null && <span>{formatDateTimeJa(item.published_at)}</span>}
        {item.language !== null && <span>原文言語: {item.language}</span>}
      </div>

      <h3 className="text-base font-semibold">{item.title}</h3>
      {item.translated_title !== null && (
        <p className="text-sm text-zinc-700 dark:text-zinc-300">{item.translated_title}</p>
      )}
      {item.summary_ja !== null && <p className="text-sm">{item.summary_ja}</p>}

      <TagList label="トピック" tags={item.topics} />
      <TagList label="技術" tags={item.technologies} />

      <p className="text-xs text-zinc-500 dark:text-zinc-400">
        推薦スコア: {item.score.toFixed(3)}
      </p>
      <ScoreBreakdown reasons={item.reasons} />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-pressed={item.feedback?.action === "good"}
          onClick={() => onFeedback("good")}
          className={FEEDBACK_BUTTON_CLASS}
        >
          Good
        </button>
        <button
          type="button"
          aria-pressed={item.feedback?.action === "bad"}
          onClick={() => setIsBadPickerOpen(true)}
          className={FEEDBACK_BUTTON_CLASS}
        >
          Bad
        </button>
        <button
          type="button"
          aria-pressed={item.feedback?.action === "save"}
          onClick={() => onFeedback("save")}
          className={FEEDBACK_BUTTON_CLASS}
        >
          保存
        </button>
        {item.feedback !== null && (
          <button
            type="button"
            onClick={onRemoveFeedback}
            className="text-xs text-zinc-500 underline dark:text-zinc-400"
          >
            フィードバックを取り消す
          </button>
        )}
        {isSafeHttpUrl(item.original_url) ? (
          <a
            href={item.original_url}
            target="_blank"
            rel="noreferrer noopener"
            className="ml-auto text-sm underline"
          >
            元記事を開く
          </a>
        ) : (
          // http/https 以外のスキームはバックエンド側の検証をすり抜けない想定だが、
          // 万一届いてもリンク化せずテキスト表示に留める（多層防御）。
          <span className="ml-auto text-sm text-zinc-400 dark:text-zinc-500">
            元記事のリンクを表示できません
          </span>
        )}
      </div>

      {isBadPickerOpen && (
        <BadReasonPicker onSubmit={handleBadSubmit} onCancel={() => setIsBadPickerOpen(false)} />
      )}
    </article>
  );
}
