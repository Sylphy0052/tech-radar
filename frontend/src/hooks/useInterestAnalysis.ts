"use client";

import { useEffect, useState } from "react";

import { getInterestSummary, getInterestTimeline, listInterestClusters } from "@/lib/interests";
import type { InterestClusterListResponse, InterestSummaryResponse, InterestTimelineResponse } from "@/lib/interests";
import { getRequestErrorMessage } from "@/lib/request-error-message";

interface UseInterestAnalysisResult {
  summary: InterestSummaryResponse | null;
  clusters: InterestClusterListResponse | null;
  timeline: InterestTimelineResponse | null;
  isLoading: boolean;
  error: string | null;
}

/**
 * 関心分析画面（Issue #16）向けの状態管理 hook（`useInterestArticles` と同じ構成）。
 *
 * summary / clusters / timeline は互いに独立した3本の GET だが、画面としては
 * 揃って初めて描画できるため `Promise.all` でまとめて取得する。フィルター等の
 * 再取得条件が無いためマウント時の1回だけ取得すればよく、`useInterestArticles`
 * のようなフィルター変化に伴う取得し直しのロジックは持たない。
 */
export function useInterestAnalysis(): UseInterestAnalysisResult {
  const [summary, setSummary] = useState<InterestSummaryResponse | null>(null);
  const [clusters, setClusters] = useState<InterestClusterListResponse | null>(null);
  const [timeline, setTimeline] = useState<InterestTimelineResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    Promise.all([getInterestSummary(), listInterestClusters(), getInterestTimeline()])
      .then(([summaryResponse, clustersResponse, timelineResponse]) => {
        if (cancelled) {
          return;
        }
        setSummary(summaryResponse);
        setClusters(clustersResponse);
        setTimeline(timelineResponse);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) {
          return;
        }
        setError(getRequestErrorMessage(err));
      })
      .finally(() => {
        if (cancelled) {
          return;
        }
        setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  return { summary, clusters, timeline, isLoading, error };
}
