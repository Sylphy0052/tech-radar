"""選好更新に共通する Bad 判定（`PROJECT_SPEC.md` §7.2）。

トピック単位（`topics.py`）と情報源単位（`sources.py`）はどちらも「1 件の Bad
だけでは抑制せず、直近フィードバック集合の中で Bad が繰り返された場合にのみ
段階的に下げる」という同じ判定を使う。判定そのものはどちらの対象にも依存しない
ため、この共有モジュールに置く（`negative_weight` をどう最終的な重みへ反映する
かは対象ごとに異なるため、各モジュールが持つ）。

判定は副作用を持たない純粋関数として実装し、DB モデルには依存させない
（`techradar.recommendation.ranking` と同じ方針）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from techradar.db.enums import FeedbackAction


@dataclass(frozen=True)
class PreferenceDecaySettings:
    """Bad の繰り返しによる選好低下の設定。

    `config.ScoringConfig.topic_preference` / `source_preference` の共通部分に
    対応する。閾値と低下量は対象（トピック / 情報源）ごとに別々の設定値から
    組み立てる（同じ値を共有するとは限らない）。
    """

    # 選好低下の判定に見る直近フィードバック件数。
    recent_window: int
    # 直近 recent_window 件中この件数以上が Bad なら選好を下げる。
    bad_threshold: int
    # 1 段階あたりの選好低下量。
    decay_step: float


def should_penalize(
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


def compute_negative_weight(
    recent_actions: Sequence[FeedbackAction], settings: PreferenceDecaySettings
) -> float:
    """直近フィードバック集合から `negative_weight` を導出する（`PROJECT_SPEC.md` §7.2）。

    「これまでの増分の累積」ではなく、「直近フィードバック集合が示す値」として
    毎回導出し直す。こうすることで、フィードバックの追加と取り消し後の再計算
    （`interest/service.py` の `recompute_topic_preferences_after_removal` /
    `recompute_source_preferences_after_removal`）の両方で同じ関数を呼べば、
    どちらの経路でも「その時点の直近集合」に対応する一意な値になる
    （状態が過去のイベント回数ではなく現在の履歴だけから定まる）。

    `should_penalize` の条件（直近 `recent_window` 件中 `bad_threshold` 件以上が
    Bad）を満たさない場合は 0（抑制なし）。満たす場合は、閾値を何段階超えているか
    （`bad_count - bad_threshold + 1`。ちょうど閾値で 1 段階、Bad が 1 件増える
    ごとに 1 段階ずつ強まる）に `decay_step` を掛けた値を返す。

    `recent_actions` は新しい順（直近のフィードバックが先頭）で受け取る前提と
    する（`should_penalize` と同じ）。
    """
    if not should_penalize(
        recent_actions,
        recent_window=settings.recent_window,
        bad_threshold=settings.bad_threshold,
    ):
        return 0.0

    window = recent_actions[: settings.recent_window]
    bad_count = sum(1 for action in window if action == FeedbackAction.BAD)
    steps = bad_count - settings.bad_threshold + 1
    return steps * settings.decay_step
