/**
 * 日時表示の共通整形処理。バックエンドは ISO 8601 の UTC 文字列
 * （例: `published_at`）をそのまま返すため、UI 側で日本語ロケール表示へ変換する。
 */

const DATE_TIME_FORMAT_OPTIONS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  // 単一ユーザー・日本語UI前提のため、実行環境のタイムゾーンに関わらず JST 固定で表示する。
  timeZone: "Asia/Tokyo",
};

/** ISO 8601 の日時文字列を `2026年8月1日 09:00` 形式（JST）へ整形する。 */
export function formatDateTimeJa(isoString: string): string {
  const date = new Date(isoString);
  return date.toLocaleString("ja-JP", DATE_TIME_FORMAT_OPTIONS);
}
