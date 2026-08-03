"""関心プロファイルの更新（`PROJECT_SPEC.md` §7, §8）。"""

from techradar.interest.clusters import (
    ClusteringSettings,
    ClusterSource,
    InterestCluster,
    build_interest_clusters,
)
from techradar.interest.preferences import (
    PreferenceDecaySettings,
    compute_negative_weight,
    should_penalize,
)
from techradar.interest.sources import SourceWeights
from techradar.interest.topics import (
    TopicWeights,
    apply_bad_feedback,
    compute_effective_weight,
    increase_positive_weight,
)
from techradar.interest.weights import (
    DEFAULT_CONFIDENCE,
    FeedbackWeights,
    compute_effective_interest,
    compute_recency_decay,
    explicit_weight_for_origin,
)

# 情報源単位の更新関数（`sources.py`）はトピック側と同名のため、ここでは値型
# （`SourceWeights`）だけを再公開する。関数は `from techradar.interest import sources`
# のようにモジュールごと取り込んで使う（どちらの選好を更新しているのかを
# 呼び出し側で読み取れるようにするため）。
__all__ = [
    "DEFAULT_CONFIDENCE",
    "ClusterSource",
    "ClusteringSettings",
    "FeedbackWeights",
    "InterestCluster",
    "PreferenceDecaySettings",
    "SourceWeights",
    "TopicWeights",
    "apply_bad_feedback",
    "build_interest_clusters",
    "compute_effective_interest",
    "compute_effective_weight",
    "compute_negative_weight",
    "compute_recency_decay",
    "explicit_weight_for_origin",
    "increase_positive_weight",
    "should_penalize",
]
