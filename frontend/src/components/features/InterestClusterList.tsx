import { InterestChartCard } from "@/components/features/InterestChartCard";
import type { InterestClusterItem } from "@/lib/interests";

interface InterestClusterListProps {
  clusters: InterestClusterItem[];
}

const EMPTY_MESSAGE = "関心クラスタはまだありません。";

/**
 * 複数の関心クラスタ（可視化 9/9、`clusters.items`）。
 *
 * クラスタはグラフより「ラベル・重み・構成トピック」をまとめて読めるカード
 * 一覧の方が向く（`centroid_embedding` を持たない閲覧用途のレスポンスであり、
 * 空間的な位置関係を可視化する情報が無いため）。受入基準「各クラスタの
 * トピックが確認できること」を満たすため、`topics` を全て表示する。
 *
 * React の key に `label` を単独で使わないのは、`user_interest_clusters.label`
 * に一意制約が無く（主キーは `id`。ラベルは上位トピックから組み立てるため、
 * 別クラスタでも上位トピックが揃えば同じ文字列になりうる）、レスポンスにも
 * `id` を含めていないため。並び順は API 側で安定しているので、インデックスを
 * 添えて衝突を避ける。
 */
export function InterestClusterList({ clusters }: InterestClusterListProps) {
  return (
    <InterestChartCard title="複数の関心クラスタ" isEmpty={clusters.length === 0} emptyMessage={EMPTY_MESSAGE}>
      <div className="grid gap-3 sm:grid-cols-2">
        {clusters.map((cluster, index) => (
          <article
            key={`${index}-${cluster.label}`}
            className="flex flex-col gap-2 border border-line bg-surface-raised p-3"
          >
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-semibold text-ink">{cluster.label}</h4>
              <span className="mono-label text-accent">重み {cluster.weight.toFixed(2)}</span>
            </div>
            <div className="flex flex-wrap gap-1">
              {cluster.topics.map((topic, topicIndex) => (
                <span key={`${topicIndex}-${topic}`} className="chip">
                  {topic}
                </span>
              ))}
            </div>
          </article>
        ))}
      </div>
    </InterestChartCard>
  );
}
