/**
 * ページングの定数とパーサ（`feed.ts` / `interest-articles.ts` の両方から使う共通実装）。
 *
 * backend は `GET /api/feed` と `GET /api/articles` の両方を単一の `MAX_PAGE_NUMBER`
 * （`api/query_filters.py`）で制約している。もとはフィード側（Issue #95）が
 * `feed.ts` に専用実装として持っていたが、関心記事一覧（`/articles`）のページ番号も
 * URL へ載せるにあたって（Issue #100）同じ制約を書き写さずこちらへ寄せた。
 */

/** 1始まりのページ番号の下限（`Pagination` と backend の `page` は 1 始まり）。 */
export const FIRST_PAGE = 1;

/**
 * ページ番号の上限（`api/query_filters.py` の `MAX_PAGE_NUMBER` と揃える）。
 *
 * backend 側は `page * limit` が bigint に収まらなくなると OFFSET で 500 になるため、
 * この値を超える `page` を 422 で弾いている（Issue #96）。UI からは押せない値だが、
 * URL は手で書けるので、送る前にここでも弾く。
 */
export const MAX_PAGE = 1_000_000;

/**
 * 10進の正の整数リテラルだけを受け付ける。`Number()` は `1e2` / `0x10` / `+3` /
 * ` 3 ` / `007` も数値へ変換するが、いずれもページ番号として打ち込まれたものでは
 * ないため、素通しさせず1ページ目へ落とす（ページャの表示と URL が食い違う）。
 * 先頭が `0` の桁を許さないので、`0` と `007` はここで落ちる。
 */
const DECIMAL_PAGE_PATTERN = /^[1-9][0-9]*$/;

/**
 * URL の `page` クエリを 1 始まりのページ番号として読む。10進の整数でない値・
 * 範囲外は1ページ目へ落とす。
 *
 * `parseMaxAgeDaysOrNull`（`feed.ts`）と同じ狙いで、壊れた URL クエリをそのまま
 * `GET /api/feed` や `GET /api/articles` へ送って 422 にしないための防御である。
 * 共有リンク・ブラウザ履歴・手動編集でクエリは容易に壊れる。「エラーを見せる」より
 * 「1ページ目を見せる」方が、共有された URL の末尾が欠けていたときの体験として
 * 素直だと判断した（Issue #95）。
 */
export function parsePageOrFirst(value: string | null): number {
  if (value === null || !DECIMAL_PAGE_PATTERN.test(value)) {
    return FIRST_PAGE;
  }
  const page = Number(value);
  if (page < FIRST_PAGE || page > MAX_PAGE) {
    return FIRST_PAGE;
  }
  return page;
}
