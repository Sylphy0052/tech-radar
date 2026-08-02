"""関心プロファイルの更新（`PROJECT_SPEC.md` §7, §8）。"""

from techradar.interest.clusters import (
    ClusteringSettings,
    ClusterSource,
    InterestCluster,
    build_interest_clusters,
)
from techradar.interest.topics import (
    TopicPreferenceSettings,
    TopicWeights,
    apply_bad_feedback,
    compute_effective_weight,
    increase_positive_weight,
    should_penalize_topic,
)
from techradar.interest.weights import (
    DEFAULT_CONFIDENCE,
    FeedbackWeights,
    compute_effective_interest,
    compute_recency_decay,
    explicit_weight_for_origin,
)

__all__ = [
    "DEFAULT_CONFIDENCE",
    "ClusterSource",
    "ClusteringSettings",
    "FeedbackWeights",
    "InterestCluster",
    "TopicPreferenceSettings",
    "TopicWeights",
    "apply_bad_feedback",
    "build_interest_clusters",
    "compute_effective_interest",
    "compute_effective_weight",
    "compute_recency_decay",
    "explicit_weight_for_origin",
    "increase_positive_weight",
    "should_penalize_topic",
]
