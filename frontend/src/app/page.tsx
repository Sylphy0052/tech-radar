import { Suspense } from "react";

import { ArticleRegistrationForm } from "@/components/features/ArticleRegistrationForm";
import { BulkArticleImportForm } from "@/components/features/BulkArticleImportForm";
import { CrawlRunPanel } from "@/components/features/CrawlRunPanel";
import { DiscoverFeed } from "@/components/features/DiscoverFeed";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { PageShell } from "@/components/ui/PageShell";

/**
 * `DiscoverFeed` は `useSearchParams` を使うクライアントコンポーネントのため、
 * `Suspense` で包む（ビルド時の "Missing Suspense boundary with useSearchParams"
 * を避けるため。詳細は `articles/page.tsx` と同じ、
 * `node_modules/next/dist/docs/01-app/03-api-reference/04-functions/use-search-params.md`）。
 */
export default function Home() {
  return (
    <PageShell
      current="feed"
      title="TechRadar"
      description="技術記事に特化したパーソナライズド・フィード。一次情報を優先して新着記事を推薦します。"
    >
      <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
        <div className="flex flex-col gap-6 lg:w-80 lg:shrink-0">
          <ArticleRegistrationForm />
          <BulkArticleImportForm />
          <CrawlRunPanel />
        </div>
        <div className="min-w-0 flex-1">
          <Suspense fallback={<LoadingIndicator label="読み込み中..." />}>
            <DiscoverFeed />
          </Suspense>
        </div>
      </div>
    </PageShell>
  );
}
