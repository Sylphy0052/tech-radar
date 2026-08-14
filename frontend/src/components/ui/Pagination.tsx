"use client";

const ELLIPSIS = "ellipsis" as const;

type PageItem = number | typeof ELLIPSIS;

interface PaginationProps {
  /** 現在のページ番号（1始まり）。 */
  currentPage: number;
  /** 総ページ数。 */
  totalPages: number;
  /** 総件数。ページ番号ボタンの有無にかかわらず表示する。 */
  totalCount: number;
  /** ページ変更時に呼ばれる。渡す値は1始まりのページ番号。 */
  onPageChange: (page: number) => void;
}

/**
 * 表示するページ番号を間引く。
 *
 * 先頭・末尾・現在ページの前後1件だけを残し、それ以外は省略記号にする
 * （現在位置が全体のどのあたりかさえ分かれば十分で、全件を並べると
 * 総ページ数が増えたときに横幅を圧迫するため）。1始まりのSetへ
 * 残したいページ番号だけを集め、昇順に並べたあと隣接しない箇所へ
 * 省略記号を挟む。
 */
function buildPageItems(currentPage: number, totalPages: number): PageItem[] {
  const keep = new Set<number>([1, totalPages]);
  for (let page = currentPage - 1; page <= currentPage + 1; page += 1) {
    if (page >= 1 && page <= totalPages) {
      keep.add(page);
    }
  }

  const sorted = Array.from(keep).sort((a, b) => a - b);
  const items: PageItem[] = [];
  let previous: number | undefined;
  for (const page of sorted) {
    if (previous !== undefined && page - previous > 1) {
      items.push(ELLIPSIS);
    }
    items.push(page);
    previous = page;
  }
  return items;
}

/**
 * 番号付きページャ。おすすめフィードと関心記事一覧の両方から使う共用部品
 * のため、取得や検索条件など画面固有の関心事は持たず、ページ番号の授受
 * だけに徹する。
 */
export function Pagination({ currentPage, totalPages, totalCount, onPageChange }: PaginationProps) {
  const countLabel = `全${totalCount}件`;

  if (totalPages <= 1) {
    return <p className="mono-label">{countLabel}</p>;
  }

  const items = buildPageItems(currentPage, totalPages);

  return (
    <nav aria-label="ページ送り" className="flex flex-wrap items-center gap-3">
      <p className="mono-label">{countLabel}</p>
      <ul className="flex items-center gap-1">
        <li>
          <button
            type="button"
            className="btn"
            aria-label="前のページへ"
            disabled={currentPage <= 1}
            onClick={() => onPageChange(currentPage - 1)}
          >
            前へ
          </button>
        </li>
        {items.map((item, index) =>
          item === ELLIPSIS ? (
            // 同じページ内に省略記号は最大2箇所（先頭側・末尾側）しか出ないため、
            // 出現位置を示す index を key に使っても入れ替わりは起きない。
            <li key={`ellipsis-${index}`} aria-hidden="true" className="px-1 text-ink-subtle">
              …
            </li>
          ) : (
            <li key={item}>
              <button
                type="button"
                className={item === currentPage ? "btn btn-primary" : "btn"}
                aria-label={`${item}ページ目へ`}
                aria-current={item === currentPage ? "page" : undefined}
                onClick={() => onPageChange(item)}
              >
                {item}
              </button>
            </li>
          ),
        )}
        <li>
          <button
            type="button"
            className="btn"
            aria-label="次のページへ"
            disabled={currentPage >= totalPages}
            onClick={() => onPageChange(currentPage + 1)}
          >
            次へ
          </button>
        </li>
      </ul>
    </nav>
  );
}
