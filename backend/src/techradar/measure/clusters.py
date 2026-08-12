"""関心クラスタの集計（Issue #74、Issue #73 の前提）。

クラスタ数の決め方（`min_clusters` / `max_clusters` / `min_articles_per_cluster`）が
実データで妥当かを判断するため、実際のクラスタ数・各クラスタの記事数・topics を出す。

クラスタの構築そのものは `interest.clusters.build_interest_clusters` を呼ぶ。計測用に
別実装を持つと、測っている対象が本番の挙動から離れる。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from techradar.interest.clusters import ClusterSource, InterestCluster


@dataclass(frozen=True)
class ClusterSummary:
    """1 クラスタ分の集計。"""

    label: str
    weight: float
    topics: tuple[str, ...]
    article_count: int


@dataclass(frozen=True)
class ClusterStats:
    """クラスタ全体の集計。`source_count` はクラスタリング対象になった記事数。"""

    source_count: int
    cluster_count: int
    clusters: tuple[ClusterSummary, ...]


def _squared_distance(left: Sequence[float], right: Sequence[float]) -> float:
    return sum((a - b) ** 2 for a, b in zip(left, right, strict=False))


def assign_sources_to_clusters(
    sources: Sequence[ClusterSource], clusters: Sequence[InterestCluster]
) -> tuple[int, ...]:
    """各記事を最も近い centroid のクラスタへ割り当てる。

    `build_interest_clusters` は割り当て自体を返さないため、ここで再現する。KMeans は
    各点を最も近い centroid へ割り当てるため、同じ距離で求めれば一致する。等距離の
    ときは添字の小さい方へ寄せ、実行のたびに結果が変わらないようにする。
    """
    if not clusters:
        return ()

    assignments = []
    for source in sources:
        best_index = 0
        best_distance = math.inf
        for index, cluster in enumerate(clusters):
            distance = _squared_distance(source.embedding, cluster.centroid)
            if distance < best_distance:
                best_index = index
                best_distance = distance
        assignments.append(best_index)
    return tuple(assignments)


def summarize_clusters(
    sources: Sequence[ClusterSource], clusters: Sequence[InterestCluster]
) -> ClusterStats:
    """クラスタごとの記事数を数えてまとめる。

    記事が割り当たらなかったクラスタも 0 件として残す。欠番にすると、クラスタ数と
    一覧の件数が食い違って読み手が混乱する。
    """
    assignments = assign_sources_to_clusters(sources, clusters)
    counts = [0] * len(clusters)
    for cluster_index in assignments:
        counts[cluster_index] += 1

    return ClusterStats(
        source_count=len(sources),
        cluster_count=len(clusters),
        clusters=tuple(
            ClusterSummary(
                label=cluster.label,
                weight=cluster.weight,
                topics=cluster.topics,
                article_count=count,
            )
            for cluster, count in zip(clusters, counts, strict=True)
        ),
    )
