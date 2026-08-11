import Link from "next/link";

/**
 * 全画面共通のシェル（Issue #38）。
 *
 * ヘッダーのブランド表記とナビゲーション、ページ見出し、本文領域の幅と余白を
 * 1か所に集約する。各 `page.tsx` はこのコンポーネントに見出しと本文を渡すだけに
 * 留め、画面ごとにレイアウトが少しずつずれるのを防ぐ。
 *
 * ナビゲーションは表示を等幅の英字にしつつ、日本語の説明を視覚的に隠した文字列
 * として併記する。`aria-label` で日本語名に差し替えてしまうと、画面に見えている
 * 「FEED」がアクセシブルネームに含まれず、音声操作で読み上げどおりに指示できなく
 * なるため（WCAG 2.5.3 Label in Name）。
 */

const NAV_ITEMS = [
  { key: "feed", href: "/", display: "FEED", label: "フィードを見る" },
  { key: "articles", href: "/articles", display: "ARTICLES", label: "関心記事一覧を見る" },
  { key: "interests", href: "/interests", display: "INTERESTS", label: "関心分析を見る" },
] as const;

export type PageKey = (typeof NAV_ITEMS)[number]["key"];

interface PageShellProps {
  /** 現在表示中の画面。ナビゲーション上で強調する対象を決める。 */
  current: PageKey;
  /** `h1` に表示する見出し。 */
  title: string;
  /** 見出し直下の説明文。 */
  description: string;
  children: React.ReactNode;
}

export function PageShell({ current, title, description, children }: PageShellProps) {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-line">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-x-6 gap-y-2 px-6 py-3">
          <Link
            href="/"
            aria-label="TechRadarのトップへ"
            className="font-mono text-sm tracking-[0.2em] text-ink"
          >
            <span className="text-accent">[</span>
            TECHRADAR
            <span className="text-accent">]</span>
          </Link>
          <nav aria-label="画面切り替え">
            <ul className="flex items-center gap-4">
              {NAV_ITEMS.map((item) => (
                <li key={item.key}>
                  <Link
                    href={item.href}
                    aria-current={item.key === current ? "page" : undefined}
                    className={`font-mono text-xs tracking-[0.12em] ${
                      item.key === current
                        ? "text-accent-strong"
                        : "text-ink-subtle hover:text-accent"
                    }`}
                  >
                    {item.display}
                    <span className="sr-only"> {item.label}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 px-6 py-10">
        <div className="flex flex-col gap-3">
          <h1 className="heading text-3xl">{title}</h1>
          <p className="max-w-2xl text-sm text-ink-muted">{description}</p>
        </div>
        {children}
      </main>
    </div>
  );
}
