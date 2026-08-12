"""関心クラスタの集計（`techradar.measure.clusters`）のテスト（Issue #74）。

クラスタ数の決め方（`min_clusters` / `max_clusters` / `min_articles_per_cluster`）が
実データで妥当かを判断するには、実際に何個のクラスタができ、各クラスタに何件の記事が
入り、どんな topics が集まったかを見る必要がある（Issue #73）。

`build_interest_clusters` は各クラスタの centroid を返すが、記事がどのクラスタへ
割り当てられたかは返さない。記事数を出すため、centroid との距離で割り当てを再現する。
KMeans は各点を最も近い centroid へ割り当てるため、同じ距離計算で再現できる。
"""

from __future__ import annotations

from techradar.interest.clusters import ClusterSource, InterestCluster
from techradar.measure.clusters import (
    ClusterStats,
    assign_sources_to_clusters,
    summarize_clusters,
)


def _source(embedding: tuple[float, ...], topics: tuple[str, ...], weight: float) -> ClusterSource:
    return ClusterSource(embedding=embedding, topics=topics, weight=weight)


def _cluster(label: str, centroid: tuple[float, ...], weight: float = 0.5) -> InterestCluster:
    return InterestCluster(label=label, weight=weight, topics=("t",), centroid=centroid)


class TestAssignSourcesToClusters:
    def test_assigns_each_source_to_the_nearest_centroid(self) -> None:
        sources = [
            _source((0.0, 0.0), ("a",), 1.0),
            _source((10.0, 10.0), ("b",), 1.0),
            _source((0.5, 0.5), ("c",), 1.0),
        ]
        clusters = [_cluster("near-origin", (0.0, 0.0)), _cluster("far", (10.0, 10.0))]

        assert assign_sources_to_clusters(sources, clusters) == (0, 1, 0)

    def test_breaks_ties_by_lowest_cluster_index(self) -> None:
        """等距離のときは添字の小さい方へ寄せる。実行のたびに結果が変わらないようにする。"""
        sources = [_source((1.0,), ("a",), 1.0)]
        clusters = [_cluster("left", (0.0,)), _cluster("right", (2.0,))]

        assert assign_sources_to_clusters(sources, clusters) == (0,)

    def test_returns_empty_without_clusters(self) -> None:
        """クラスタが無ければ割り当ても無い。記事があっても失敗させない。"""
        assert assign_sources_to_clusters([_source((0.0,), ("a",), 1.0)], []) == ()


class TestSummarizeClusters:
    def test_returns_empty_stats_without_sources(self) -> None:
        stats = summarize_clusters([], [])

        assert stats == ClusterStats(source_count=0, cluster_count=0, clusters=())

    def test_counts_articles_per_cluster(self) -> None:
        sources = [
            _source((0.0, 0.0), ("kubernetes",), 1.0),
            _source((0.1, 0.1), ("kubernetes", "helm"), 0.5),
            _source((9.0, 9.0), ("rust",), 0.8),
        ]
        clusters = [
            _cluster("container", (0.05, 0.05), weight=0.7),
            _cluster("language", (9.0, 9.0), weight=0.3),
        ]

        stats = summarize_clusters(sources, clusters)

        assert stats.source_count == 3
        assert stats.cluster_count == 2
        assert [(c.label, c.article_count) for c in stats.clusters] == [
            ("container", 2),
            ("language", 1),
        ]

    def test_keeps_label_weight_and_topics(self) -> None:
        """ラベル・重み・topics はクラスタ構築の結果をそのまま伝える。"""
        sources = [_source((0.0,), ("go",), 1.0)]
        clusters = [
            InterestCluster(
                label="Go / Concurrency",
                weight=0.62,
                topics=("Go", "Concurrency"),
                centroid=(0.0,),
            )
        ]

        stats = summarize_clusters(sources, clusters)

        assert stats.clusters[0].label == "Go / Concurrency"
        assert stats.clusters[0].weight == 0.62
        assert stats.clusters[0].topics == ("Go", "Concurrency")

    def test_reports_zero_for_clusters_without_members(self) -> None:
        """どの記事も割り当たらないクラスタも 0 件として残す。欠番にすると総数が合わなくなる。"""
        sources = [_source((0.0,), ("a",), 1.0)]
        clusters = [_cluster("used", (0.0,)), _cluster("unused", (100.0,))]

        stats = summarize_clusters(sources, clusters)

        assert [(c.label, c.article_count) for c in stats.clusters] == [("used", 1), ("unused", 0)]
