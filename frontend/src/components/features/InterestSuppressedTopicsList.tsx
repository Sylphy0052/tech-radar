import { InterestChartCard } from "@/components/features/InterestChartCard";
import type { SuppressedTopicItem } from "@/lib/interests";

interface InterestSuppressedTopicsListProps {
  suppressedTopics: SuppressedTopicItem[];
}

const EMPTY_MESSAGE = "抑制中のトピックはありません。";

/**
 * 抑制中のジャンル（可視化 8/9、`summary.suppressed_topics`）。
 *
 * グラフではなく一覧表示にする（`negative_weight` そのものはトピック間で
 * 比較する意味が薄く、「何が・どれだけ抑制されているか」を読めれば十分なため）。
 * 受入基準「抑制中であることが文言で明示されること」を満たすため、各行に
 * 「抑制中」という文言を必ず出す（アイコンや色だけに頼らない）。
 */
export function InterestSuppressedTopicsList({ suppressedTopics }: InterestSuppressedTopicsListProps) {
  return (
    <InterestChartCard
      title="抑制中のジャンル"
      isEmpty={suppressedTopics.length === 0}
      emptyMessage={EMPTY_MESSAGE}
    >
      <ul className="flex flex-col gap-2">
        {suppressedTopics.map((item) => (
          <li
            key={item.topic}
            className="flex flex-wrap items-center justify-between gap-2 border border-line bg-surface-raised px-3 py-2"
          >
            <span className="chip">{item.topic}</span>
            <span className="mono-label text-warn">
              抑制中（抑制度 {item.negative_weight.toFixed(2)}）
            </span>
          </li>
        ))}
      </ul>
    </InterestChartCard>
  );
}
