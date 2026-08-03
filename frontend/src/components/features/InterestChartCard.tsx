import type { ReactNode } from "react";

interface InterestChartCardProps {
  title: string;
  /** グラフの前提（集計対象の絞り込み等）を補足する短い説明。省略可。 */
  description?: string;
  /** true のときはグラフを描かず空状態を表示する（受入基準「データが空のときに壊れず空状態を表示する」）。 */
  isEmpty: boolean;
  emptyMessage?: string;
  children: ReactNode;
}

const DEFAULT_EMPTY_MESSAGE = "まだデータがありません。";

/**
 * 関心分析画面（Issue #16）の可視化カード共通の枠。
 *
 * 見出し・空状態の出し分けを9種の可視化コンポーネントで毎回書かせず、ここへ
 * 集約する（DRY）。`isEmpty` を呼び出し側で判定させるのは、「0件」の定義が
 * グラフごとに異なるため（例: 円グラフは合計0件、棒グラフは要素数0件）。
 */
export function InterestChartCard({
  title,
  description,
  isEmpty,
  emptyMessage,
  children,
}: InterestChartCardProps) {
  return (
    <section className="panel flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h3 className="heading text-base">{title}</h3>
        {description !== undefined && <p className="text-xs text-ink-muted">{description}</p>}
      </div>
      {isEmpty ? (
        <p className="text-sm text-ink-muted">{emptyMessage ?? DEFAULT_EMPTY_MESSAGE}</p>
      ) : (
        children
      )}
    </section>
  );
}
