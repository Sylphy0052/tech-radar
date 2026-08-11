"""関心クラスタの構築（`PROJECT_SPEC.md` §8）。

ユーザーの関心は単一の平均 Embedding ではなく複数クラスタとして保持する
（§8）。クラスタリングには scikit-learn の KMeans を使う。入出力はいずれも
`@dataclass(frozen=True)` の値型にし、DB モデルには依存させない
（`techradar.recommendation.ranking` と同じ方針）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sklearn.cluster import KMeans

# KMeans の初期化を複数回試して最良の結果を採る回数。random_state を固定していても
# 初期値候補の探索回数自体は品質に影響するため、sklearn のバージョン間で既定値が
# 変わっても挙動が変化しないよう明示する。
_KMEANS_N_INIT = 10


@dataclass(frozen=True)
class ClusterSource:
    """クラスタリング対象の 1 記事分の情報。"""

    embedding: tuple[float, ...]
    topics: tuple[str, ...]
    # effective_interest（`interest.weights.compute_effective_interest`）。
    # クラスタの weight 算出に使う。
    weight: float


@dataclass(frozen=True)
class InterestCluster:
    """構築済みの関心クラスタ（`PROJECT_SPEC.md` §8 の `interest_clusters` 要素）。"""

    label: str
    weight: float
    topics: tuple[str, ...]
    centroid: tuple[float, ...]


@dataclass(frozen=True)
class ClusteringSettings:
    """関心クラスタ構築（KMeans）の設定（`config.ScoringConfig.clustering` 相当）。"""

    min_clusters: int
    max_clusters: int
    # 1 クラスタを成立させるのに要する記事数。
    min_articles_per_cluster: int
    # ラベルに使うトピック語数。
    label_topic_count: int
    # KMeans の初期化を固定し、実行のたびに結果が変わらないようにする。
    random_state: int


def _cluster_count(article_count: int, settings: ClusteringSettings) -> int:
    """記事数と設定からクラスタ数 k を決める。

    `min_articles_per_cluster`（1 クラスタを成立させるのに要する記事数）を
    `min_clusters`（クラスタ数の下限）より優先する。以前は
    `max(min_clusters, capacity_based)` としており、既定値
    （min_clusters=2, min_articles_per_cluster=3）で記事が 4〜5 件のときに
    2 クラスタへ割ってしまい、1 クラスタあたりの記事数が
    min_articles_per_cluster を割り込んでいた（Issue #15 自己レビュー 2）。
    「記事が少ないうちは無理にクラスタを分けない」方が自然なため、記事数から
    賄えるクラスタ数（`capacity_based = article_count // min_articles_per_cluster`）
    を超えては分けない。

    `min_clusters` は capacity_based がそれを満たせる記事数
    （`article_count >= min_clusters * min_articles_per_cluster`）になった
    時点で自動的に満たされる「目安の下限」であり、capacity_based を上回って
    まで強制するものではない（capacity_based が min_clusters 未満のときは
    capacity_based をそのまま採用する）。

    `min_articles_per_cluster` に満たない少数の記事しか無い場合は、複数クラスタに
    分けるだけの材料が無いため 1 クラスタに丸める（例外にしない）。それ以外は
    capacity_based を `max_clusters` に収め、記事数を超えないようにする
    （KMeans は `n_clusters` が標本数を超えるとエラーになるため）。
    """
    if article_count <= 0:
        return 0
    if article_count < settings.min_articles_per_cluster:
        return 1

    capacity_based = article_count // settings.min_articles_per_cluster
    bounded = min(capacity_based, settings.max_clusters)
    return min(bounded, article_count)


def _top_topics(topics: Sequence[str], count: int) -> tuple[str, ...]:
    """トピックを頻度集計し、上位 `count` 語を返す。

    同数の場合は語の辞書順にすることで、実行のたびに結果が変わらないようにする。
    """
    if not topics:
        return ()
    counts = Counter(topics)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(term for term, _ in ranked[:count])


def _label_from_top_topics(top_topics: tuple[str, ...], fallback_index: int) -> str:
    """上位トピックを `" / "` で連結してラベルにする。

    `topics` が空で他と衝突しないラベルを作れない記事群の場合は、クラスタの
    通し番号を含む一意なフォールバック文字列にする。
    """
    if not top_topics:
        return f"未分類クラスタ #{fallback_index}"
    return " / ".join(top_topics)


def _normalized_weight(cluster_weight_sum: float, total_weight: float, cluster_count: int) -> float:
    """クラスタの weight を全クラスタ合計が 1.0 になるよう正規化する。

    `total_weight` が 0（全記事の effective_interest が 0）の場合は比較の
    基準が無いため、クラスタ数で均等按分する。
    """
    if total_weight <= 0.0:
        return 1.0 / cluster_count
    return cluster_weight_sum / total_weight


def build_interest_clusters(
    sources: Sequence[ClusterSource], settings: ClusteringSettings
) -> tuple[InterestCluster, ...]:
    """関心クラスタ群を構築する。

    記事が 0 件なら空を返す。KMeans で `embedding` を分類し、クラスタごとに
    `weight`（記事の `weight` 合計の正規化値）とラベル（クラスタ内トピックの
    上位語を連結した文字列）を求める。並び順は weight 降順、同値は label 昇順
    にすることで、実行のたびに順序が変わらないようにする。
    """
    if not sources:
        return ()

    k = _cluster_count(len(sources), settings)
    embeddings = [list(source.embedding) for source in sources]
    model = KMeans(n_clusters=k, random_state=settings.random_state, n_init=_KMEANS_N_INIT)
    assignments = model.fit_predict(embeddings)
    centroids = model.cluster_centers_

    members_by_cluster: dict[int, list[int]] = defaultdict(list)
    for index, cluster_id in enumerate(assignments):
        members_by_cluster[int(cluster_id)].append(index)

    total_weight = sum(source.weight for source in sources)
    cluster_count = len(members_by_cluster)

    clusters = []
    for fallback_index, (cluster_id, member_indices) in enumerate(
        sorted(members_by_cluster.items())
    ):
        members = [sources[i] for i in member_indices]
        cluster_weight_sum = sum(member.weight for member in members)
        top_topics = _top_topics(
            [topic for member in members for topic in member.topics], settings.label_topic_count
        )
        clusters.append(
            InterestCluster(
                label=_label_from_top_topics(top_topics, fallback_index),
                weight=_normalized_weight(cluster_weight_sum, total_weight, cluster_count),
                topics=top_topics,
                centroid=tuple(float(value) for value in centroids[cluster_id]),
            )
        )

    return tuple(sorted(clusters, key=lambda cluster: (-cluster.weight, cluster.label)))
