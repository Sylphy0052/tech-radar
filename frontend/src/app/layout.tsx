import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "TechRadar",
  description: "一次情報を優先した技術記事のパーソナライズド・フィード",
};

/*
 * ダーク固定（Issue #38）。ブラウザのUI（スクロールバー・フォームの既定色・
 * モバイルのアドレスバー）まで暗い前提に揃える。`globals.css` の
 * `color-scheme: dark` と同じ意図を、初回描画前に効く形で宣言している。
 */
export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#05080c",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="ja"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
