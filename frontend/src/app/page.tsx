export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-4 p-8 font-sans">
      <h1 className="text-3xl font-bold">TechRadar</h1>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        技術記事に特化したパーソナライズド・フィード。一次情報を優先して新着記事を推薦します。
      </p>
      <p className="text-sm text-zinc-500">
        画面はIssue #12以降で実装します。バックエンドの稼働確認は{" "}
        <code className="rounded bg-zinc-100 px-1 py-0.5 dark:bg-zinc-800">GET /api/health</code>{" "}
        を参照してください。
      </p>
    </main>
  );
}
