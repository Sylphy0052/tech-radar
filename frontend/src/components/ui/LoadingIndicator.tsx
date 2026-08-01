interface LoadingIndicatorProps {
  label: string;
}

/**
 * 画面共通のローディング表示。各機能でバラバラにマークアップさせず、ここへ集約する。
 */
export function LoadingIndicator({ label }: LoadingIndicatorProps) {
  return (
    <p role="status" className="text-sm text-zinc-500 dark:text-zinc-400">
      {label}
    </p>
  );
}
