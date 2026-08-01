import { ArticleRegistrationForm } from "@/components/features/ArticleRegistrationForm";
import { CrawlRunPanel } from "@/components/features/CrawlRunPanel";
import { DiscoverFeed } from "@/components/features/DiscoverFeed";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-4xl flex-col gap-8 p-8 font-sans">
      <div className="flex flex-col gap-4">
        <h1 className="text-3xl font-bold">TechRadar</h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          技術記事に特化したパーソナライズド・フィード。一次情報を優先して新着記事を推薦します。
        </p>
      </div>

      <div className="flex flex-col gap-8 md:flex-row md:items-start">
        <div className="flex flex-col gap-8 md:w-80 md:shrink-0">
          <ArticleRegistrationForm />
          <CrawlRunPanel />
        </div>
        <div className="min-w-0 flex-1">
          <DiscoverFeed />
        </div>
      </div>
    </main>
  );
}
