"""情報源単位の選好更新（`user_source_preferences` の計算、`PROJECT_SPEC.md` §7.1 手順 4）。

Good / 保存はその記事の情報源（`articles.source_domain`）への `positive_weight` を
増やし、Bad は「1 件では効かせない」ため直近フィードバック集合から
`negative_weight` を導出する（`preferences.compute_negative_weight`）。トピック単位
の選好（`topics.py`）と同じ構造で、Bad 判定は `preferences.py` を共有する。

選好のキーは `source_registry.id` ではなく `articles.source_domain` にする。
レジストリに登録の無いドメインの記事にも選好を持たせる必要があるため
（`source_registry` は公式ソースだけを収録しており、一般のブログ等は載らない）。

判定・更新はいずれも副作用を持たない純粋関数として実装し、DB モデルには
依存させない（`techradar.recommendation.ranking` と同じ方針）。選好を推薦
スコアへ反映する係数の計算は採点側の関心事のため
`recommendation/ranking.py` の `compute_source_preference_factor` が持つ。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from techradar.db.enums import FeedbackAction
from techradar.interest.preferences import PreferenceDecaySettings, compute_negative_weight


@dataclass(frozen=True)
class SourceWeights:
    """情報源単位の選好（`user_source_preferences` の 3 列に対応する値型）。"""

    positive: float
    negative: float
    effective: float


def compute_effective_weight(positive: float, negative: float) -> float:
    """`positive` と `negative` から `effective_weight` を導出する。

    トピック側（`topics.py`）の「`negative` を `positive` に掛ける減衰係数として
    扱う」方式は採らず、符号付きの引き算にする。減衰係数方式では
    `effective = positive / (1 + negative)` となり、Good された履歴が無い情報源
    （`positive` が 0）は `negative` がいくら増えても `effective` が 0 のまま
    動かない。情報源は「一度も Good していないが Bad は繰り返している」状態が
    普通に起こりうる（関心の薄い情報源が候補に混ざり続けるため）ので、その
    状態を抑制として表現できない形は要件を満たさない。

    引き算にすることで `effective` は負値を取りうるが、推薦スコアへ反映する
    際は `recommendation/ranking.py` の `compute_source_preference_factor` が
    `min_factor` / `max_factor` で挟むため、抑制はしても情報源の権威性を完全に
    ゼロにはしない（`topics.py` と同じく §6.1 の既読減点の設計思想に揃う）。
    """
    return positive - negative


def increase_positive_weight(current: SourceWeights, increment: float) -> SourceWeights:
    """Good 系フィードバックで情報源の `positive_weight` を増やす（`PROJECT_SPEC.md` §7.1 手順 4）。

    `increment` は呼び出し側が `feedback_weights`（Good: 0.8 等）から決める。
    """
    new_positive = current.positive + increment
    return SourceWeights(
        positive=new_positive,
        negative=current.negative,
        effective=compute_effective_weight(new_positive, current.negative),
    )


def apply_bad_feedback(
    current: SourceWeights,
    recent_actions: Sequence[FeedbackAction],
    settings: PreferenceDecaySettings,
) -> SourceWeights:
    """Bad フィードバックを反映する（`PROJECT_SPEC.md` §7.2 と同じ考え方）。

    `negative_weight` は `preferences.compute_negative_weight` が直近フィードバック
    集合から導出する値へ置き換える（`current.negative` への加算ではない）。値が
    変わらない場合（条件未達のまま、または既に同じ値）は `current` をそのまま
    返す（1 件の Bad だけでは情報源全体を抑制しない。無用な更新もしない）。

    取り消し後の再計算（`interest/service.py` の
    `recompute_source_preferences_after_removal`）も同じ `compute_negative_weight`
    を使うため、増加方向（この関数）と取り消し後の再計算とで結果が食い違わない。
    """
    new_negative = compute_negative_weight(recent_actions, settings)
    if new_negative == current.negative:
        return current

    return SourceWeights(
        positive=current.positive,
        negative=new_negative,
        effective=compute_effective_weight(current.positive, new_negative),
    )
