import { InterestAnalysisDashboard } from "@/components/features/InterestAnalysisDashboard";
import { PageShell } from "@/components/ui/PageShell";

/**
 * 関心分析画面（Issue #16、`PROJECT_SPEC.md` §7, §8）。
 *
 * `InterestAnalysisDashboard` は `useInterestAnalysis`（`useSearchParams` を
 * 使わない単純な mount 時取得）だけに依存するため、`articles/page.tsx` と
 * 異なり `Suspense` は不要（`useSearchParams` を使うコンポーネントだけが
 * 対象、詳細は `node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-search-params.md`）。
 */
export default function InterestsPage() {
  return (
    <PageShell
      current="interests"
      title="関心分析"
      description="フィードバックと登録履歴から、関心の傾向・時間変化・クラスタをまとめて確認できます。"
    >
      <InterestAnalysisDashboard />
    </PageShell>
  );
}
