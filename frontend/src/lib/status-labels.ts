/**
 * `JobStatus`（backend `db/enums.py`）および
 * `RegistrationErrorReason`（backend `jobs/handlers/errors.py`）の値を
 * ユーザー向けの日本語表示へ写像する。
 *
 * backend は分類値（英語の識別子）をそのまま返す設計のため（`error_reason` は
 * 例外メッセージそのものを含めない代わりに、この粒度の値を返す）、UI 側で
 * 一箇所に集約して日本語化する。未知の値が来ても例外にせず、汎用メッセージへ
 * フォールバックする。
 */

const TERMINAL_STATUSES = ["completed", "failed"] as const;

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

const REGISTRATION_STATUS_LABELS: Record<string, string> = {
  pending: "登録待ち",
  fetching: "記事を取得中",
  analyzing: "記事を解析中",
  searching: "関連情報を検索中",
  completed: "登録完了",
  failed: "登録失敗",
};

const FALLBACK_REGISTRATION_STATUS_LABEL = "処理中";

export function getRegistrationStatusLabel(status: string): string {
  return REGISTRATION_STATUS_LABELS[status] ?? FALLBACK_REGISTRATION_STATUS_LABEL;
}

const JOB_STATUS_LABELS: Record<string, string> = {
  pending: "実行待ち",
  searching: "巡回実行中",
  fetching: "実行中",
  analyzing: "実行中",
  completed: "巡回完了",
  failed: "巡回失敗",
};

const FALLBACK_JOB_STATUS_LABEL = "実行中";

export function getJobStatusLabel(status: string): string {
  return JOB_STATUS_LABELS[status] ?? FALLBACK_JOB_STATUS_LABEL;
}

const REGISTRATION_ERROR_MESSAGES: Record<string, string> = {
  fetch_failed: "記事の取得に失敗しました。URLを確認して再度お試しください。",
  extraction_failed: "記事本文を取り出せませんでした。別のURLをお試しください。",
  analysis_failed: "記事の解析に失敗しました。しばらくしてから再度お試しください。",
  embedding_failed: "記事の登録処理に失敗しました。しばらくしてから再度お試しください。",
};

const FALLBACK_REGISTRATION_ERROR_MESSAGE =
  "登録処理に失敗しました。しばらくしてから再度お試しください。";

/**
 * `error_reason` をユーザー向けメッセージへ変換する。null・未知の値でも
 * 汎用メッセージにフォールバックし、分類値をそのまま画面に出さない。
 */
export function getRegistrationErrorMessage(errorReason: string | null): string {
  if (errorReason === null) {
    return FALLBACK_REGISTRATION_ERROR_MESSAGE;
  }
  return REGISTRATION_ERROR_MESSAGES[errorReason] ?? FALLBACK_REGISTRATION_ERROR_MESSAGE;
}
