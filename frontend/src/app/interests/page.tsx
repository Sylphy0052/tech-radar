import { InterestAnalysisDashboard } from "@/components/features/InterestAnalysisDashboard";

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
    <main className="mx-auto flex min-h-screen max-w-4xl flex-col gap-8 p-8 font-sans">
      <div className="flex flex-col gap-4">
        <h1 className="text-3xl font-bold">関心分析</h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          フィードバックと登録履歴から、関心の傾向・時間変化・クラスタをまとめて確認できます。
        </p>
      </div>

      <InterestAnalysisDashboard />
    </main>
  );
}
