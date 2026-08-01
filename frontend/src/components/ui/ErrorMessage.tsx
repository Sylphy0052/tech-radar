interface ErrorMessageProps {
  message: string;
}

/**
 * 画面共通のエラー表示。各機能でバラバラにマークアップさせず、ここへ集約する。
 */
export function ErrorMessage({ message }: ErrorMessageProps) {
  return (
    <p
      role="alert"
      className="rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
    >
      {message}
    </p>
  );
}
