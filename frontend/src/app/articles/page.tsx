import { Suspense } from "react";

import { ArticleFilterPanel } from "@/components/features/ArticleFilterPanel";
import { InterestArticleList } from "@/components/features/InterestArticleList";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { PageShell } from "@/components/ui/PageShell";

/**
 * 関心記事一覧画面（`PROJECT_SPEC.md` §6.3）。
 *
 * `ArticleFilterPanel` / `InterestArticleList` はどちらも `useSearchParams` を
 * 使うクライアントコンポーネントのため、`Suspense` で包む（ビルド時の
 * "Missing Suspense boundary with useSearchParams" を避けるため。詳細は
 * `node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-search-params.md`）。
 */
export default function ArticlesPage() {
  return (
    <PageShell
      current="articles"
      title="関心記事一覧"
      description="手動登録・Good・保存した記事をまとめて確認し、条件で絞り込めます。"
    >
      <Suspense fallback={<LoadingIndicator label="読み込み中..." />}>
        <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
          <div className="flex flex-col gap-4 lg:w-80 lg:shrink-0">
            <ArticleFilterPanel />
          </div>
          <div className="min-w-0 flex-1">
            <InterestArticleList />
          </div>
        </div>
      </Suspense>
    </PageShell>
  );
}
