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
      className="border-l-2 border-danger bg-surface-raised py-2 pl-3 pr-3 text-sm text-danger"
    >
      {message}
    </p>
  );
}
