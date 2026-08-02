"""トピック単位の選好更新（`user_topic_preferences` の計算、`PROJECT_SPEC.md` §7.1, §7.2）。

Good 系フィードバックはトピックの `positive_weight` を増やす。Bad は「1 件では
効かせない」ため、直近フィードバック履歴のうち一定割合が Bad のときだけ
`negative_weight` を段階的に増やす。判定・更新はいずれも副作用を持たない純粋関数
として実装し、DB モデルには依存させない（`techradar.recommendation.ranking` と
同じ方針）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from techradar.db.enums import FeedbackAction


@dataclass(frozen=True)
class TopicWeights:
    """トピック単位の選好（`user_topic_preferences` の 3 列に対応する値型）。"""

    positive: float
    negative: float
    effective: float


@dataclass(frozen=True)
class TopicPreferenceSettings:
    """トピック単位の選好更新の設定（`config.ScoringConfig.topic_preference` 相当）。"""

    # トピック重み低下の判定に見る直近フィードバック件数。
    recent_window: int
    # 直近 recent_window 件中この件数以上が Bad ならトピック重みを下げる。
    bad_threshold: int
    # 1 段階あたりのトピック重み低下量。
    decay_step: float


def should_penalize_topic(
    recent_actions: Sequence[FeedbackAction],
    *,
    recent_window: int,
    bad_threshold: int,
) -> bool:
    """直近 `recent_window` 件中 Bad が `bad_threshold` 件以上かを判定する。

    `PROJECT_SPEC.md` §7.2「一件のBadだけでジャンル全体を抑制しない。同一ジャンルで
    Badが繰り返された場合のみ、ジャンル重みを段階的に下げる」の判定部分。

    `recent_actions` は新しい順（直近のフィードバックが先頭）で受け取る前提と
    する。`recent_window` より件数が少ない場合は、渡された全件を対象にする。
    """
    window = recent_actions[:recent_window]
    bad_count = sum(1 for action in window if action == FeedbackAction.BAD)
    return bad_count >= bad_threshold


def compute_effective_weight(positive: float, negative: float) -> float:
    """`positive` と `negative` から `effective_weight` を導出する。

    単純な `positive - negative` にはしない。Bad は「そのトピックへの関心を
    打ち消す」というより「そのトピックを勧めることへの確信を弱める」性質が強い
    （`PROJECT_SPEC.md` §7.2 も「意味的に近い記事を抑制する」という書きぶりで、
    関心の否定ではなく抑制として扱っている）。単純な引き算だと `positive` が
    大きい人気トピックでも Bad 数件で `effective` が容易に 0 以下へ落ち込み、
    他項目の寄与では挽回できなくなる。そこで `negative` を `positive` に掛ける
    減衰係数として扱うことで、`effective` は常に `[0, positive]` に収まり、
    「抑制はするが完全排除はしない」という §6.1 の既読減点と同じ設計思想に揃える。
    """
    decay_factor = 1.0 / (1.0 + negative)
    return positive * decay_factor


def increase_positive_weight(current: TopicWeights, increment: float) -> TopicWeights:
    """Good 系フィードバックでトピックの `positive_weight` を増やす（`PROJECT_SPEC.md` §7.1）。

    `increment` は呼び出し側が `feedback_weights`（Good: 0.8 等）から決める。
    """
    new_positive = current.positive + increment
    return TopicWeights(
        positive=new_positive,
        negative=current.negative,
        effective=compute_effective_weight(new_positive, current.negative),
    )


def apply_bad_feedback(
    current: TopicWeights,
    recent_actions: Sequence[FeedbackAction],
    settings: TopicPreferenceSettings,
) -> TopicWeights:
    """Bad フィードバックを反映する（`PROJECT_SPEC.md` §7.2）。

    `should_penalize_topic` の条件を満たしたときだけ `negative_weight` を
    `decay_step` 分だけ増やす。条件を満たさない場合は `current` をそのまま返す
    （1 件の Bad だけではトピック全体を抑制しない）。
    """
    penalize = should_penalize_topic(
        recent_actions,
        recent_window=settings.recent_window,
        bad_threshold=settings.bad_threshold,
    )
    if not penalize:
        return current

    new_negative = current.negative + settings.decay_step
    return TopicWeights(
        positive=current.positive,
        negative=new_negative,
        effective=compute_effective_weight(current.positive, new_negative),
    )
