"""関心クラスタの構築を検証する（`PROJECT_SPEC.md` §8）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.interest.clusters import (
    ClusteringSettings,
    ClusterSource,
    _cluster_count,
    build_interest_clusters,
)

SETTINGS = ClusteringSettings(
    min_clusters=2,
    max_clusters=8,
    min_articles_per_cluster=3,
    label_topic_count=3,
    random_state=0,
)


def make_source(
    *, embedding: tuple[float, ...], topics: tuple[str, ...] = (), weight: float = 1.0
) -> ClusterSource:
    """テスト用の `ClusterSource` を作る。"""
    return ClusterSource(embedding=embedding, topics=topics, weight=weight)


class TestClusterCountPrioritizesCapacity:
    """`min_articles_per_cluster` を `min_clusters` より優先することを固定する。

    既定値（min_clusters=2, min_articles_per_cluster=3）で記事が 4〜5 件のとき、
    以前は `max(min_clusters, capacity_based)` で 2 クラスタに割ってしまい、
    1 クラスタあたりの記事数が min_articles_per_cluster を割り込んでいた
    （Issue #15 自己レビュー 2）。
    """

    def test_does_not_reach_min_clusters_when_capacity_is_insufficient(self):
        # Arrange — capacity_based = 4 // 3 = 1 < min_clusters(2)
        # Act / Assert — min_clusters を無理に満たさず 1 クラスタのまま
        assert _cluster_count(4, SETTINGS) == 1
        assert _cluster_count(5, SETTINGS) == 1

    def test_reaches_min_clusters_once_capacity_allows(self):
        # Arrange — capacity_based = 6 // 3 = 2 == min_clusters(2)
        # Act / Assert — capacity が満たせるようになった時点で min_clusters に届く
        assert _cluster_count(6, SETTINGS) == 2

    def test_build_interest_clusters_keeps_a_single_cluster_below_capacity(self):
        """`build_interest_clusters` 経由でも 1 クラスタのまま分割されないことを確認する。"""
        # Arrange — 明確に離れた2群でも記事数（4件）が capacity を満たさない
        sources = (
            make_source(embedding=(10.0, 10.0, 10.0), topics=("AI",)),
            make_source(embedding=(10.0, 10.0, 10.0), topics=("AI",)),
            make_source(embedding=(-10.0, -10.0, -10.0), topics=("DevOps",)),
            make_source(embedding=(-10.0, -10.0, -10.0), topics=("DevOps",)),
        )
        # Act
        clusters = build_interest_clusters(sources, SETTINGS)
        # Assert
        assert len(clusters) == 1


class TestEmptyAndSmallInputs:
    def test_returns_empty_for_no_sources(self):
        # Arrange / Act
        clusters = build_interest_clusters((), SETTINGS)
        # Assert
        assert clusters == ()

    def test_returns_a_single_cluster_when_articles_are_too_few(self):
        # Arrange — min_articles_per_cluster（3）に満たない記事数
        sources = (
            make_source(embedding=(1.0, 0.0), topics=("MCP",)),
            make_source(embedding=(0.0, 1.0), topics=("MCP",)),
        )
        # Act
        clusters = build_interest_clusters(sources, SETTINGS)
        # Assert — 例外にならず 1 クラスタになる
        assert len(clusters) == 1
        assert clusters[0].weight == pytest.approx(1.0)


class TestClusterSeparation:
    def test_two_well_separated_groups_form_at_least_two_clusters(self):
        # Arrange — 明確に離れた 2 群
        group_a = [make_source(embedding=(10.0, 10.0, 10.0), topics=("AI",)) for _ in range(4)]
        group_b = [
            make_source(embedding=(-10.0, -10.0, -10.0), topics=("DevOps",)) for _ in range(4)
        ]
        # Act
        clusters = build_interest_clusters(tuple(group_a + group_b), SETTINGS)
        # Assert
        assert len(clusters) >= 2
        total_weight = sum(cluster.weight for cluster in clusters)
        assert total_weight == pytest.approx(1.0, abs=1e-9)

    def test_cluster_weight_reflects_the_articles_effective_interest(self):
        # Arrange — 一方の群の weight を大きくする
        heavy_group = [
            make_source(embedding=(10.0, 10.0, 10.0), topics=("AI",), weight=3.0) for _ in range(4)
        ]
        light_group = [
            make_source(embedding=(-10.0, -10.0, -10.0), topics=("DevOps",), weight=1.0)
            for _ in range(4)
        ]
        # Act
        clusters = build_interest_clusters(tuple(heavy_group + light_group), SETTINGS)
        # Assert — 最も weight が大きいクラスタが先頭に来る（weight 降順）
        assert clusters[0].weight > clusters[1].weight
        assert clusters[0].label == "AI"


class TestDeterminism:
    def test_same_input_yields_the_same_result_twice(self):
        # Arrange
        sources = tuple(
            make_source(embedding=(float(i), float(-i), 0.0), topics=(f"topic-{i % 3}",))
            for i in range(9)
        )
        # Act
        first = build_interest_clusters(sources, SETTINGS)
        second = build_interest_clusters(sources, SETTINGS)
        # Assert
        assert first == second


class TestLabeling:
    def test_label_uses_top_topics_joined_by_slash(self):
        # Arrange — min_articles_per_cluster 未満にして 1 クラスタに固定する
        sources = (
            make_source(embedding=(1.0, 1.0, 1.0), topics=("MCP", "MCP", "Tool Use")),
            make_source(embedding=(1.0, 1.0, 1.0), topics=("MCP", "MCP", "Tool Use")),
        )
        # Act
        clusters = build_interest_clusters(sources, SETTINGS)
        # Assert — 頻度順（MCP が 4 回、Tool Use が 2 回）
        assert clusters[0].label == "MCP / Tool Use"
        assert set(clusters[0].topics) == {"MCP", "Tool Use"}

    def test_label_falls_back_to_a_unique_string_when_topics_are_empty(self):
        # Arrange
        sources = (
            make_source(embedding=(1.0, 0.0, 0.0), topics=()),
            make_source(embedding=(1.0, 0.0, 0.0), topics=()),
        )
        # Act
        clusters = build_interest_clusters(sources, SETTINGS)
        # Assert
        assert clusters[0].label
        assert clusters[0].topics == ()
