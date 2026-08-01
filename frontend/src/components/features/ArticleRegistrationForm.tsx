"use client";

import { useId, useState, type FormEvent } from "react";

import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { usePolling } from "@/hooks/usePolling";
import type { ArticleRegistration } from "@/lib/articles";
import { getArticleRegistration, registerArticle } from "@/lib/articles";
import { getRequestErrorMessage } from "@/lib/request-error-message";
import {
  getRegistrationErrorMessage,
  getRegistrationStatusLabel,
  isTerminalStatus,
} from "@/lib/status-labels";
import { validateArticleUrl } from "@/lib/url-validation";

function isRegistrationTerminal(registration: ArticleRegistration): boolean {
  return isTerminalStatus(registration.status);
}

/**
 * URL 登録フォーム。送信前にクライアント側でスキームを検証し、登録後は
 * 登録状態を（pending → fetching → analyzing → completed/failed へ）
 * ポーリングで追従表示する。
 */
export function ArticleRegistrationForm() {
  const inputId = useId();
  const [url, setUrl] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [initialRegistration, setInitialRegistration] = useState<ArticleRegistration | null>(
    null,
  );
  const [registrationId, setRegistrationId] = useState<string | null>(null);

  const polling = usePolling(registrationId, getArticleRegistration, {
    isTerminal: isRegistrationTerminal,
  });

  // POST 直後の応答をすぐ表示し、以降はポーリングの最新値へ切り替える。
  const registration = polling.data ?? initialRegistration;

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();

    const message = validateArticleUrl(url);
    if (message !== null) {
      setValidationError(message);
      return;
    }

    setValidationError(null);
    setSubmitError(null);
    // 同じ URL を登録し直すと backend は既存の登録をそのまま返す。id が
    // 変わらないとポーリングは再開されないため、送信のたびに一度切っておく。
    setRegistrationId(null);
    setIsSubmitting(true);
    try {
      const result = await registerArticle(url.trim());
      setInitialRegistration(result);
      setRegistrationId(result.id);
    } catch (error) {
      setSubmitError(getRequestErrorMessage(error));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">記事URLを登録</h2>
      <form onSubmit={handleSubmit} className="flex flex-col gap-2">
        <label htmlFor={inputId} className="text-sm font-medium">
          記事のURL
        </label>
        <input
          id={inputId}
          type="text"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://example.com/articles/1"
          className="rounded border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900"
        />
        <button
          type="submit"
          disabled={isSubmitting}
          className="self-start rounded bg-zinc-900 px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          登録する
        </button>
      </form>

      {validationError !== null && <ErrorMessage message={validationError} />}
      {submitError !== null && <ErrorMessage message={submitError} />}

      {registration !== null && (
        <div className="flex flex-col gap-1 rounded border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
          <p>登録したURL: {registration.url}</p>
          <p>状態: {getRegistrationStatusLabel(registration.status)}</p>
          {registration.status === "failed" && (
            <ErrorMessage message={getRegistrationErrorMessage(registration.error_reason)} />
          )}
        </div>
      )}

      {polling.isLoading && registration === null && (
        <LoadingIndicator label="登録状況を確認しています..." />
      )}
      {polling.error !== null && <ErrorMessage message={getRequestErrorMessage(polling.error)} />}
    </section>
  );
}
