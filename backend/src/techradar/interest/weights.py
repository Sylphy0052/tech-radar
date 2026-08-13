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

# 確信度（confidence）の上限。全てのシグナルが揃った記事は補正を受けない。
MAX_CONFIDENCE = 1.0


@dataclass(frozen=True)
class ConfidenceSettings:
    """確信度の各シグナルの寄与（`config.ScoringConfig.confidence` 相当）。

    3 つのシグナルの寄与の合計が `MAX_CONFIDENCE`（1.0）になるよう設定側で
    検証する（`recommendation/config.py` の `ConfidenceConfig`）。
    """

    # embedding があるときの寄与。
    has_embedding: float
    # topics があるときの寄与。
    has_topics: float
    # 解析が完了しているときの寄与。
    is_analyzed: float
    # 全てのシグナルが欠けていても下回らない下限。
    min_confidence: float


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


def compute_confidence(
    *,
    has_embedding: bool,
    has_topics: bool,
    is_analyzed: bool,
    settings: ConfidenceSettings,
) -> float:
    """記事のシグナル充足度から確信度を返す（`PROJECT_SPEC.md` §8、Issue #20）。

    `confidence` は「その記事がユーザーの関心をどれだけ確かに表すか」を表す。
    フィードバックの経路の強さ（`explicit_weight`）や新しさ（`recency_decay`）
    とは独立に、その記事について手元にある情報がどれだけ揃っているかで決める。

    * `has_embedding`: 関心プロファイル（`recommendation/service.py` の
      `build_interest_profile`）と関心クラスタへ寄与できるか
    * `has_topics`: トピック選好へ寄与できるか
    * `is_analyzed`: 解析（`analysis/service.py`）が完了しているか。未解析の
      記事は topics も embedding も後から付くため、現時点の情報は暫定である

    クリックされただけで解析前の記事は全て欠けるが、それでもユーザーがその記事へ
    到達したこと自体は事実のため、寄与をゼロにはせず `min_confidence` を下限に
    する（「抑制はするが完全排除はしない」という §6.1 の既読減点と同じ考え方）。

    設定側で寄与の合計が 1.0 になるよう検証しているが、純粋関数としては設定が
    どうであれ係数が 1.0 を超えないよう上限で丸める。
    """
    satisfied = (
        (settings.has_embedding if has_embedding else 0.0)
        + (settings.has_topics if has_topics else 0.0)
        + (settings.is_analyzed if is_analyzed else 0.0)
    )
    return min(max(satisfied, settings.min_confidence), MAX_CONFIDENCE)


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
