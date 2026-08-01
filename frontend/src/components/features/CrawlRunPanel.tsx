"use client";

import { useState } from "react";

import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { usePolling } from "@/hooks/usePolling";
import { startCrawlRun } from "@/lib/crawl";
import type { Job } from "@/lib/jobs";
import { getJob } from "@/lib/jobs";
import { getRequestErrorMessage } from "@/lib/request-error-message";
import { getJobStatusLabel, isTerminalStatus } from "@/lib/status-labels";

function isJobTerminal(job: Job): boolean {
  return isTerminalStatus(job.status);
}

/**
 * 巡回実行ボタン。実行中（起動リクエスト送信中、またはジョブが終端状態に
 * 達していない間）は多重起動できないようボタンを無効化する。backend 側も
 * 重複起動を弾くが、UI 側でも連打を防ぐ。
 */
export function CrawlRunPanel() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const polling = usePolling(jobId, getJob, { isTerminal: isJobTerminal });

  const isJobRunning = jobId !== null && !(polling.data !== null && isJobTerminal(polling.data));
  const isButtonDisabled = isStarting || isJobRunning;

  async function handleClick(): Promise<void> {
    setStartError(null);
    setIsStarting(true);
    try {
      const result = await startCrawlRun();
      setJobId(result.job_id);
    } catch (error) {
      setStartError(getRequestErrorMessage(error));
    } finally {
      setIsStarting(false);
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">情報源の巡回</h2>
      <button
        type="button"
        onClick={handleClick}
        disabled={isButtonDisabled}
        className="self-start rounded bg-zinc-900 px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
      >
        巡回を実行
      </button>

      {startError !== null && <ErrorMessage message={startError} />}
      {polling.data !== null && <p>状態: {getJobStatusLabel(polling.data.status)}</p>}
      {polling.error !== null && <ErrorMessage message={getRequestErrorMessage(polling.error)} />}
    </section>
  );
}
