"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import { ErrorMessage } from "@/components/ui/ErrorMessage";
import { LoadingIndicator } from "@/components/ui/LoadingIndicator";
import { Pagination } from "@/components/ui/Pagination";
import { useInterestArticles } from "@/hooks/useInterestArticles";
import { formatDateTimeJa } from "@/lib/format-date";
import {
  CONTENT_TYPE_LABELS,
  originLabel,
  parseArticleFiltersFromSearchParams,
  TECHNOLOGY_STATUS_LABELS,
  technologyDisplayStatus,
} from "@/lib/interest-articles";
import type { InterestArticleItem } from "@/lib/interest-articles";
import { FIRST_PAGE, parsePageOrFirst } from "@/lib/pagination";
import { isSafeHttpUrl } from "@/lib/safe-url";

function contentTypeLabel(contentType: string | null): string | null {
  if (contentType === null) {
    return null;
  }
  return CONTENT_TYPE_LABELS[contentType] ?? contentType;
}

interface InterestArticleCardProps {
  item: InterestArticleItem;
  onRemove: () => void;
}

/** 記事1件分のカード。関心記事一覧の表示項目（`PROJECT_SPEC.md` §6.3）をまとめる。 */
function InterestArticleCard({ item, onRemove }: InterestArticleCardProps) {
  const contentType = contentTypeLabel(item.content_type);
  const technologyStatus = technologyDisplayStatus(item.analysis_status, item.technologies);

  return (
    <article className="panel panel-interactive flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="chip">{originLabel(item.origin)}</span>
        {item.is_primary_source && <span className="chip chip-accent">公式・一次情報</span>}
        <span aria-hidden="true" className="text-ink-subtle">
          ::
        </span>
        <span className="font-mono text-ink-muted">{formatDateTimeJa(item.registered_at)}</span>
        <span aria-hidden="true" className="text-ink-subtle">
          ·
        </span>
        <span className="font-mono text-ink-muted">{item.source_domain}</span>
        {contentType !== null && (
          <>
            <span aria-hidden="true" className="text-ink-subtle">
              ·
            </span>
            <span className="chip">{contentType}</span>
          </>
        )}
      </div>

      <h3 className="text-base font-semibold text-ink">{item.title}</h3>
      {item.translated_title !== null && (
        <p className="text-sm text-ink-muted">{item.translated_title}</p>
      )}

      {item.topics.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="mono-label">トピック:</span>
          {item.topics.map((topic) => (
            <span key={topic} className="chip">
              {topic}
            </span>
          ))}
        </div>
      )}

      {/* トピックと違い、この行は常に出す（Issue #92）。タグが0件でも
          「未解析だから空」（pending/analyzing/null）と「解析済みだが実際に
          0件」（completed）を区別できるようにするため、行ごと消さない。 */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="mono-label">技術:</span>
        {technologyStatus === "tags" ? (
          item.technologies.map((technology) => (
            <span key={technology} className="chip">
              {technology}
            </span>
          ))
        ) : (
          <span className="text-sm text-ink-subtle">
            {TECHNOLOGY_STATUS_LABELS[technologyStatus]}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-1">
        <button type="button" onClick={onRemove} className="btn">
          関心対象から除外
        </button>
        {isSafeHttpUrl(item.original_url) ? (
          <a
            href={item.original_url}
            target="_blank"
            rel="noreferrer noopener"
            className="link-inline ml-auto text-sm"
          >
            元記事を開く
          </a>
        ) : (
          <span className="ml-auto text-sm text-ink-subtle">元記事のリンクを表示できません</span>
        )}
      </div>
    </article>
  );
}

/**
 * 関心記事一覧本体（`PROJECT_SPEC.md` §6.3）。
 *
 * フィルターは URL クエリから読む。`ArticleFilterPanel` と同じ
 * `parseArticleFiltersFromSearchParams` を使うため、フィルター条件を
 * コンポーネント間で二重管理することにならない（URL が唯一の情報源）。
 *
 * ページ番号もフィルターと同じく URL クエリを唯一の情報源にする（Issue #100、
 * フィード側は Issue #95 で対応済み）。`useInterestArticles` は URL から読んだ
 * ページ番号を受け取るだけで、自分では保持しない（`DiscoverFeed` と同じ設計）。
 *
 * `ArticleFilterPanel` は `DiscoverFeed` が内部で `FeedFilterPanel` を抱えるのとは
 * 違い、本コンポーネントの兄弟として `app/articles/page.tsx` からレンダーされる。
 * `ArticleFilterPanel` は `buildSearchParamsFromFilters` で URL を組み立て直すため
 * （`page` を含めない限り）フィルター変更時に `page` は自然と URL から落ちる。
 */
export function InterestArticleList() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const filters = useMemo(() => parseArticleFiltersFromSearchParams(searchParams), [searchParams]);
  const page = parsePageOrFirst(searchParams.get("page"));

  // ページ移動は履歴へ積む（`push`）。フィルターの変更が `router.replace`（
  // `ArticleFilterPanel`）なのとあえて分けている。3ページ目から戻ったら2ページ目へ
  // 帰るのが、ページャを操作した側の期待だと判断した（`DiscoverFeed` と同じ、
  // Issue #95）。
  //
  // `scroll: false` を付けるのは、書き換え前（ローカル state の更新）と同じく
  // スクロール位置を動かさないため。`push` の既定は先頭へスクロールする。
  const handlePageChange = useCallback(
    (nextPage: number) => {
      if (nextPage === page) {
        // 今見ているページ番号のボタンは押せる（`Pagination` が無効化するのは
        // 「前へ」「次へ」だけ）。ここで弾かないと、URL が変わらないまま履歴だけが
        // 積まれ、戻るボタンを余計に押さないと前のページへ帰れなくなる
        // （`DiscoverFeed` と同じガード）。
        return;
      }

      const params = new URLSearchParams(searchParams);
      if (nextPage === FIRST_PAGE) {
        // 1ページ目では `page` を URL に出さない（既定値を URL に出さないのは
        // `buildSearchParamsFromFilters` の他の条件と同じ扱い）。
        params.delete("page");
      } else {
        params.set("page", String(nextPage));
      }
      const query = params.toString();
      router.push(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [page, pathname, router, searchParams],
  );

  const { items, isLoading, error, totalPages, totalCount, removeArticle } = useInterestArticles(
    filters,
    page,
  );

  return (
    <section className="flex flex-col gap-4">
      <h2 className="heading text-lg">関心記事一覧</h2>

      {isLoading ? (
        <LoadingIndicator label="読み込み中..." />
      ) : (
        <>
          {/* エラーは読み込み中には出さない。ページ移動では直前のエラーが
              残ったままなので、外に置くと「読み込み中」と並んで出る
              （`DiscoverFeed` と同じ扱い）。 */}
          {error !== null && <ErrorMessage message={error} />}

          {items.length > 0 ? (
            <div className="flex flex-col gap-4">
              {items.map((item) => (
                <InterestArticleCard
                  key={item.article_id}
                  item={item}
                  onRemove={() => removeArticle(item.article_id)}
                />
              ))}
            </div>
          ) : (
            <p className="text-sm text-ink-muted">
              {/* 総件数が 0 のときと、そのページだけ空のときを区別する
                  （`DiscoverFeed` と同じ扱い）。 */}
              {totalCount === 0
                ? "該当する記事がありません。"
                : "このページには記事がありません。他のページを開いてください。"}
            </p>
          )}

          <Pagination
            currentPage={page}
            totalPages={totalPages}
            totalCount={totalCount}
            onPageChange={handlePageChange}
          />
        </>
      )}
    </section>
  );
}
