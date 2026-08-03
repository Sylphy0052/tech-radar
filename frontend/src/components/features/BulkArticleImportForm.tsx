"use client";

import { useId, useRef, useState, type FormEvent } from "react";

import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import type { BulkArticleImportResult } from "@/lib/articles";
import { bulkImportArticles } from "@/lib/articles";
import { getBulkImportErrorMessage } from "@/lib/bulk-import-error-message";

const NO_FILE_SELECTED_MESSAGE = "ファイルを選択してください";

/**
 * URL リストファイル（.md/.txt）の一括アップロードフォーム（Issue #39）。
 *
 * `ArticleRegistrationForm`（単一URL登録）とは完全に独立させる。一括登録は
 * backend がファイルを最後まで処理してから結果をまとめて返す 1 リクエスト完結型のため、
 * `ArticleRegistrationForm` のような登録状態のポーリングは不要。
 */
export function BulkArticleImportForm() {
  const inputId = useId();
  // <input type="file"> の選択値は React の controlled value にできない
  // （セキュリティ上、value をプログラムから任意の値に設定できない）ため、
  // 送信時に読み取り・成功後にリセットする目的で ref を使う。
  const inputRef = useRef<HTMLInputElement>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [result, setResult] = useState<BulkArticleImportResult | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();

    // 送信ボタンは isSubmitting 中 disabled になり二重送信を防ぐ
    // （ArticleRegistrationForm と同じ作法。disabled のボタンは click も
    // Enter キーによる暗黙的送信も発火しないため、ここでの追加ガードは不要）。
    const file = inputRef.current?.files?.[0] ?? null;
    if (file === null) {
      setValidationError(NO_FILE_SELECTED_MESSAGE);
      return;
    }

    setValidationError(null);
    setSubmitError(null);
    // 前回の結果を、今回の送信中の表示に紛れ込ませない。
    setResult(null);
    setIsSubmitting(true);
    try {
      const response = await bulkImportArticles(file);
      setResult(response);
      // 同じファイル名を選び直しても onChange が発火するよう、選択状態を戻す
      // （<input type="file"> の value は空文字を代入する以外に消せない）。
      if (inputRef.current !== null) {
        inputRef.current.value = "";
      }
    } catch (error) {
      setSubmitError(getBulkImportErrorMessage(error));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">URLリストを一括登録</h2>
      <form onSubmit={handleSubmit} className="flex flex-col gap-2">
        <label htmlFor={inputId} className="text-sm font-medium">
          URLリストファイル（.md / .txt）
        </label>
        <input
          id={inputId}
          ref={inputRef}
          type="file"
          accept=".md,.txt"
          className="text-sm text-zinc-700 dark:text-zinc-300"
        />
        <button
          type="submit"
          disabled={isSubmitting}
          className="self-start rounded bg-zinc-900 px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          アップロードする
        </button>
      </form>

      {validationError !== null && <ErrorMessage message={validationError} />}
      {submitError !== null && <ErrorMessage message={submitError} />}
      {isSubmitting && <LoadingIndicator label="アップロード中です..." />}

      {result !== null && (
        <div className="flex flex-col gap-2 rounded border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
          <p>
            登録 {result.created_count}件 / 重複 {result.duplicate_count}件 / エラー{" "}
            {result.error_count}件
          </p>
          {result.errors.length > 0 && (
            <ul className="flex flex-col gap-1">
              {result.errors.map((errorItem) => (
                <li
                  key={`${errorItem.line_number}-${errorItem.line}`}
                  className="text-red-700 dark:text-red-300"
                >
                  {errorItem.line_number}行目: {errorItem.reason}（{errorItem.line}）
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
