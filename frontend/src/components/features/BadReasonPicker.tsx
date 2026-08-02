"use client";

import { useId, useState } from "react";

import { BAD_REASON_LABELS } from "@/lib/feedback";
import type { BadReason } from "@/lib/feedback";

interface BadReasonPickerProps {
  /** 理由を選んでいなければ `undefined` を渡す（受入基準「Bad理由未選択でもBadが成立する」）。 */
  onSubmit: (reason?: BadReason) => void;
  onCancel: () => void;
}

/**
 * Bad の理由選択UI。理由は任意項目のため、選ばずに送信する操作も用意する。
 */
export function BadReasonPicker({ onSubmit, onCancel }: BadReasonPickerProps) {
  const groupName = useId();
  const [selectedReason, setSelectedReason] = useState<BadReason | null>(null);

  return (
    <div
      role="group"
      aria-label="Badの理由を選択"
      className="flex flex-col gap-2 rounded border border-zinc-200 p-3 text-sm dark:border-zinc-800"
    >
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs text-zinc-500 dark:text-zinc-400">
          理由を選択（任意）
        </legend>
        {Object.entries(BAD_REASON_LABELS).map(([reason, label]) => (
          <label key={reason} className="flex items-center gap-2">
            <input
              type="radio"
              name={groupName}
              value={reason}
              checked={selectedReason === reason}
              onChange={() => setSelectedReason(reason as BadReason)}
            />
            {label}
          </label>
        ))}
      </fieldset>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => onSubmit(selectedReason ?? undefined)}
          className="rounded bg-zinc-900 px-3 py-1 text-white dark:bg-zinc-100 dark:text-zinc-900"
        >
          {selectedReason === null ? "理由なしで送信" : "この理由で送信"}
        </button>
        <button type="button" onClick={onCancel} className="rounded border border-zinc-300 px-3 py-1 dark:border-zinc-700">
          閉じる
        </button>
      </div>
    </div>
  );
}
