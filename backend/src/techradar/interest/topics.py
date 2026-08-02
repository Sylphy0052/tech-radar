"""トピック単位の選好更新（`user_topic_preferences` の計算、`PROJECT_SPEC.md` §7.1, §7.2）。

Good 系フィードバックはトピックの `positive_weight` を増やす（累積加算）。Bad は
「1 件では効かせない」ため、`negative_weight` を「直近フィードバック履歴の
累積加算」ではなく「直近フィードバック集合から一意に導出する値」として扱う
（`compute_negative_weight`）。加算方式にしないのは、フィードバックの取り消し
（`interest/service.py` の `recompute_topic_preferences_after_removal`）でも
同じ関数を使って再計算できるようにするため。取り消しで直近集合が変われば
`negative_weight` もその集合が示す値へ一致し、取り消しても抑制が残り続ける
ことがない（Issue #15 自己レビュー 1）。判定・更新はいずれも副作用を持たない
純粋関数として実装し、DB モデルには依存させない（`techradar.recommendation.ranking`
と同じ方針）。
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


def compute_negative_weight(
    recent_actions: Sequence[FeedbackAction], settings: TopicPreferenceSettings
) -> float:
    """直近フィードバック集合から `negative_weight` を導出する（`PROJECT_SPEC.md` §7.2）。

    「これまでの増分の累積」ではなく、「直近フィードバック集合が示す値」として
    毎回導出し直す。こうすることで、フィードバックの追加（`apply_bad_feedback`）
    と取り消し後の再計算（`interest/service.py` の
    `recompute_topic_preferences_after_removal`）の両方で同じ関数を呼べば、
    どちらの経路でも「その時点の直近集合」に対応する一意な値になる
    （状態が過去のイベント回数ではなく現在の履歴だけから定まる）。

    `should_penalize_topic` の条件（直近 `recent_window` 件中 `bad_threshold`
    件以上が Bad）を満たさない場合は 0（抑制なし）。満たす場合は、閾値を
    何段階超えているか（`bad_count - bad_threshold + 1`。ちょうど閾値で
    1 段階、Bad が 1 件増えるごとに 1 段階ずつ強まる）に `decay_step` を
    掛けた値を返す。

    `recent_actions` は新しい順（直近のフィードバックが先頭）で受け取る前提と
    する（`should_penalize_topic` と同じ）。
    """
    if not should_penalize_topic(
        recent_actions,
        recent_window=settings.recent_window,
        bad_threshold=settings.bad_threshold,
    ):
        return 0.0

    window = recent_actions[: settings.recent_window]
    bad_count = sum(1 for action in window if action == FeedbackAction.BAD)
    steps = bad_count - settings.bad_threshold + 1
    return steps * settings.decay_step


def apply_bad_feedback(
    current: TopicWeights,
    recent_actions: Sequence[FeedbackAction],
    settings: TopicPreferenceSettings,
) -> TopicWeights:
    """Bad フィードバックを反映する（`PROJECT_SPEC.md` §7.2）。

    `negative_weight` は `compute_negative_weight` が直近フィードバック集合
    から導出する値へ置き換える（`current.negative` への加算ではない）。値が
    変わらない場合（条件未達のまま、または既に同じ値）は `current` をそのまま
    返す（1 件の Bad だけではトピック全体を抑制しない。無用な更新もしない）。

    取り消し後の再計算（`recompute_topic_preferences_after_removal`）も同じ
    `compute_negative_weight` を使うため、増加方向（この関数）と取り消し後の
    再計算とで結果が食い違わない。
    """
    new_negative = compute_negative_weight(recent_actions, settings)
    if new_negative == current.negative:
        return current

    return TopicWeights(
        positive=current.positive,
        negative=new_negative,
        effective=compute_effective_weight(current.positive, new_negative),
    )
