"""関心の実効重み計算（`PROJECT_SPEC.md` §7.1, §8）。

ユーザーの関心プロファイルは単一の重みではなく、明示的な重み・フィードバックの
強さ・時間減衰・確信度の積で表す（§8 の `effective_interest` の式）。判定は
副作用を持たない純粋関数として実装し、DB モデルには依存させない
（`techradar.recommendation.ranking` と同じ方針）。`ArticleOrigin`（`db.enums`）は
DB スキーマそのものではなく列挙値なので、依存を許容する。
"""

from __future__ import annotations

from dataclasses import dataclass

from techradar.db.enums import ArticleOrigin

# 確信度（confidence）の実値算出は Issue #20 のスコープ。本 Issue（#15 段階 1）では
# 未実装のため、常にこの既定値（＝確信度による補正なし）を渡す。
DEFAULT_CONFIDENCE = 1.0


@dataclass(frozen=True)
class FeedbackWeights:
    """フィードバック種別ごとの重み（`PROJECT_SPEC.md` §7.1, §7.2 の重み表）。

    `config.ScoringConfig.feedback_weights`（Pydantic）に対応する値型。純粋関数
    側は Pydantic に依存させないため、呼び出し側がここへ変換して渡す。
    """

    manual: float
    good: float
    save: float
    read_full: float
    clicked: float
    bad: float


# 関心記事の追加経路（`ArticleOrigin`）と `FeedbackWeights` の対応するフィールド名。
# `ArticleOrigin` には Bad に対応する値が無い（Bad は関心記事への追加ではなく
# 抑制の経路のため）。
_ORIGIN_TO_WEIGHT_FIELD: dict[ArticleOrigin, str] = {
    ArticleOrigin.MANUAL: "manual",
    ArticleOrigin.GOOD: "good",
    ArticleOrigin.SAVED: "save",
    ArticleOrigin.READ_FULL: "read_full",
    ArticleOrigin.CLICKED: "clicked",
}


def compute_recency_decay(age_days: float, half_life_days: float) -> float:
    """半減期による指数減衰を返す（`PROJECT_SPEC.md` §8 の `recency_decay`）。

    0 日で 1.0、`half_life_days` 日が経過するごとに半分になる。`age_days` が
    負（クロックずれ等による未来日時）の場合は減衰していないものとして 1.0 に
    丸める。
    """
    if age_days <= 0.0:
        return 1.0
    return 0.5 ** (age_days / half_life_days)


def compute_effective_interest(
    *,
    explicit_weight: float,
    feedback_weight: float,
    recency_decay: float,
    confidence: float,
) -> float:
    """`effective_interest` を返す（`PROJECT_SPEC.md` §8 の式）。

    `effective_interest = explicit_weight × feedback_weight × recency_decay ×
    confidence`。各項が乗算のため、いずれかが 0 に近づけば全体も 0 に近づく
    （古い関心ほど `recency_decay` が小さくなり、`effective_interest` も
    単調に小さくなる）。
    """
    return explicit_weight * feedback_weight * recency_decay * confidence


def explicit_weight_for_origin(origin: ArticleOrigin, weights: FeedbackWeights) -> float:
    """関心記事の追加経路（`origin`）から `explicit_weight` を引く（`PROJECT_SPEC.md` §7.1）。"""
    field_name = _ORIGIN_TO_WEIGHT_FIELD[origin]
    return getattr(weights, field_name)
