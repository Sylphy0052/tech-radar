"use client";

import { InterestClusterList } from "@/components/features/InterestClusterList";
import { InterestContentTypeChart } from "@/components/features/InterestContentTypeChart";
import { InterestDifficultyChart } from "@/components/features/InterestDifficultyChart";
import { InterestFeedbackRatioChart } from "@/components/features/InterestFeedbackRatioChart";
import { InterestGenreChart } from "@/components/features/InterestGenreChart";
import { InterestOriginChart } from "@/components/features/InterestOriginChart";
import { InterestPrimarySourceChart } from "@/components/features/InterestPrimarySourceChart";
import { InterestSuppressedTopicsList } from "@/components/features/InterestSuppressedTopicsList";
import { InterestTechnologyChart } from "@/components/features/InterestTechnologyChart";
import { InterestTimelineChart } from "@/components/features/InterestTimelineChart";
import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { useInterestAnalysis } from "@/hooks/useInterestAnalysis";

/**
 * 関心分析画面（Issue #16）の本体。
 *
 * `useInterestAnalysis` で summary/clusters/timeline をまとめて1回取得し、10種の
 * 可視化コンポーネントへ振り分ける。各コンポーメントが個別に取得すると3本の
 * GET が10回に膨れるため、ここで一括取得してから props で配る。
 *
 * 内容分布系（技術・公式情報比率・記事の性質・難易度）は Good・保存した記事
 * のみを対象にした集計のため、まとめて後半のグループに置き、母集団の注記は
 * 各カード（`InterestChartCard` の `description`）に出す。
 *
 * `InterestOriginChart`（寄与元、Issue #92）は前半のグループに置く。内容分布系
 * とは別の観点（登録経路の内訳）であり、前半の他のカード（ジャンル・Good/Bad
 * 比率・時間変化・クラスタ）と同じく Good・保存に絞らない全体像を示すため。
 * 表示先を1箇所に集約する（画面のあちこちに分散させない）。
 */
export function InterestAnalysisDashboard() {
  const { summary, clusters, timeline, isLoading, error } = useInterestAnalysis();

  if (isLoading) {
    return <LoadingIndicator label="読み込み中..." />;
  }

  if (summary === null || clusters === null || timeline === null) {
    // 取得に失敗した場合はエラー表示だけを出す（部分的なデータでグラフを
    // 描こうとすると、欠けた集計を欠けたまま見せてしまい誤解を招くため）。
    return error !== null ? <ErrorMessage message={error} /> : null;
  }

  return (
    <div className="flex flex-col gap-6">
      {error !== null && <ErrorMessage message={error} />}

      <div className="grid gap-6 md:grid-cols-2">
        <InterestGenreChart genres={summary.genres} />
        <InterestFeedbackRatioChart feedbackRatio={summary.feedback_ratio} />
        <InterestTimelineChart buckets={timeline.buckets} />
        <InterestClusterList clusters={clusters.items} />
        <InterestOriginChart originCounts={summary.origin_counts} />
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <InterestTechnologyChart technologies={summary.technologies} />
        <InterestPrimarySourceChart primarySourceRatio={summary.primary_source_ratio} />
        <InterestContentTypeChart contentTypes={summary.content_types} />
        <InterestDifficultyChart difficulties={summary.difficulties} />
      </div>

      <InterestSuppressedTopicsList suppressedTopics={summary.suppressed_topics} />
    </div>
  );
}
