import { ArticleRegistrationForm } from "@/components/features/ArticleRegistrationForm";
import { CrawlRunPanel } from "@/components/features/CrawlRunPanel";
import { DiscoverFeed } from "@/components/features/DiscoverFeed";
import { PageShell } from "@/components/ui/PageShell";

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
          <CrawlRunPanel />
        </div>
        <div className="min-w-0 flex-1">
          <DiscoverFeed />
        </div>
      </div>
    </PageShell>
  );
}
