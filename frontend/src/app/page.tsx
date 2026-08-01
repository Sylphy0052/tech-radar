import { ArticleRegistrationForm } from "@/components/features/ArticleRegistrationForm";
import { CrawlRunPanel } from "@/components/features/CrawlRunPanel";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 p-8 font-sans">
      <div className="flex flex-col gap-4">
        <h1 className="text-3xl font-bold">TechRadar</h1>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          技術記事に特化したパーソナライズド・フィード。一次情報を優先して新着記事を推薦します。
        </p>
      </div>

      <ArticleRegistrationForm />
      <CrawlRunPanel />
    </main>
  );
}
