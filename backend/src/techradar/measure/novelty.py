"""novelty 分布の集計（Issue #87）。

`compute_novelty` が topics の文字列一致で判定していた頃、既知トピックと 1 語も
重ならない候補が軒並み novelty = 1.0（上端）へ張り付き、diversity 枠が構造的に
選ばれなくなっていた。式は embedding のコサイン類似ベースへ差し替えたが
（`docs/adr/0007-embedding-based-novelty.md`）、同じ縮退が
別の形で再発しうるため、分布を継続して観測できるようにこの集計を残す。novelty の
計算方法そのものには関与しない。

出すのは 2 つ。ひとつは `rank_candidates` が返す `ScoredCandidate.breakdown.novelty`
の分布（`summarize_novelty_distribution`）。上端への張り付き件数・割合と、現行の
`exploration_min_novelty` を超える件数を必ず含める。もうひとつは、その閾値を
0.0〜1.0 の 0.1 刻みで動かしたときに exploration 枠と diversity 枠がどう分岐するかの表
（`summarize_threshold_table`）で、閾値を実測で確定させる材料になる。

枠の判定は `recommendation.composition._slot_for` をそのまま呼ぶ。優先順位
（strong_interest → primary_source → exploration → diversity）をここで再実装すると、
測っている対象が本番の挙動から外れるため。本番の実装を読む方針そのものは
`feed_slots.py` と同じだが、あちらが公開の `CompositionStats` だけを読むのに対し、
ここは private な `_slot_for` を跨いで呼んでいる点が違う。`composition.py` 側で
名前やシグネチャが変わっても型チェックでは気付けないため、そのときはこのモジュールの
テストが落ちることで検出する。

`_slot_for` は候補を 1 件ずつ分類するだけで、`compose_feed_with_stats` が行う定員の
適用・他枠からの補充・ドメイン重複の排除（`_select_primary`）は通らない。閾値表が
示すのは「その閾値なら各枠にいくつの候補が入りうるか」であって、最終的なフィードの
中身ではない。実際の充足率は `feed_slots.py` の集計で見る。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from techradar.recommendation.composition import FeedSlot, _slot_for
from techradar.recommendation.ranking import ScoredCandidate, ScoringSettings

# novelty の値域の上端（`NoveltyConfig` / `WeightsConfig` と同じ `le=1.0`）。
# これに張り付いている件数が Issue #87 の核心。
_SATURATION_VALUE = 1.0

# 閾値走査の刻み。yaml/コード中のリテラルと同じ浮動小数表現に揃えるため、割り算では
# なく直接書く（`i / 10` は多くの値で一致するが、比較境界のずれを避けたい）。
_THRESHOLD_CANDIDATES: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


@dataclass(frozen=True)
class NoveltyDistribution:
    """novelty 値の分布。候補が 0 件なら分布系は `None` になる。"""

    candidate_count: int
    min_novelty: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p95: float | None
    max_novelty: float | None
    # 上端 (1.0) への張り付き。
    saturated_count: int
    saturated_ratio: float
    # 現行の exploration_min_novelty 以上の候補数（`_slot_for` と同じ `>=` で判定）。
    above_threshold_count: int
    exploration_min_novelty: float


@dataclass(frozen=True)
class ThresholdSlotCounts:
    """ある novelty 閾値を仮に使ったときの exploration / diversity 枠の件数。"""

    threshold: float
    exploration_count: int
    diversity_count: int


@dataclass(frozen=True)
class NoveltyStats:
    """novelty 分布と、閾値走査の表をまとめた集計。"""

    distribution: NoveltyDistribution
    threshold_table: tuple[ThresholdSlotCounts, ...]


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """線形補間で分位点を求める。

    `statistics` モジュールには任意の分位点を 1 つだけ取れる関数が無いため自前で
    持つ。件数が 1 件のときは補間する余地が無いためその値をそのまま返す。
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = position - lower_index
    return (
        sorted_values[lower_index]
        + (sorted_values[upper_index] - sorted_values[lower_index]) * weight
    )


def summarize_novelty_distribution(
    values: Sequence[float], *, exploration_min_novelty: float
) -> NoveltyDistribution:
    """novelty 値の列から分布をまとめる。

    最小 / 四分位 / p95 / 最大に加え、上端への張り付き件数・割合を必ず出す
    （Issue #87 の核心）。現行の `exploration_min_novelty` 以上の件数もあわせて出し、
    閾値を動かす前の基準値として使えるようにする。
    """
    if not values:
        return NoveltyDistribution(
            candidate_count=0,
            min_novelty=None,
            p25=None,
            p50=None,
            p75=None,
            p95=None,
            max_novelty=None,
            saturated_count=0,
            saturated_ratio=0.0,
            above_threshold_count=0,
            exploration_min_novelty=exploration_min_novelty,
        )

    ordered = sorted(values)
    saturated_count = sum(1 for value in ordered if value >= _SATURATION_VALUE)
    above_threshold_count = sum(1 for value in ordered if value >= exploration_min_novelty)

    return NoveltyDistribution(
        candidate_count=len(ordered),
        min_novelty=_percentile(ordered, 0.0),
        p25=_percentile(ordered, 0.25),
        p50=_percentile(ordered, 0.5),
        p75=_percentile(ordered, 0.75),
        p95=_percentile(ordered, 0.95),
        max_novelty=_percentile(ordered, 1.0),
        saturated_count=saturated_count,
        saturated_ratio=saturated_count / len(ordered),
        above_threshold_count=above_threshold_count,
        exploration_min_novelty=exploration_min_novelty,
    )


def summarize_threshold_table(
    scored: Sequence[ScoredCandidate], settings: ScoringSettings
) -> tuple[ThresholdSlotCounts, ...]:
    """`exploration_min_novelty` を 0.0〜1.0 の 0.1 刻みで動かした表を作る。

    各刻みについて、`_slot_for` を（閾値だけを差し替えた `ScoringSettings` で）
    そのまま呼び、exploration 枠と diversity 枠に入る件数を数える。
    `_slot_for` は strong_interest → primary_source → exploration → diversity の
    優先順位で判定するため、strong_interest / primary_source に先に該当する候補は、
    閾値を動かしても exploration / diversity には移らない（表の合計が候補総数と
    一致しないのはこのため）。
    """
    rows = []
    for threshold in _THRESHOLD_CANDIDATES:
        trial_settings = replace(
            settings,
            feed_composition=replace(settings.feed_composition, exploration_min_novelty=threshold),
        )
        exploration_count = 0
        diversity_count = 0
        for item in scored:
            slot = _slot_for(item, trial_settings)
            if slot is FeedSlot.EXPLORATION:
                exploration_count += 1
            elif slot is FeedSlot.DIVERSITY:
                diversity_count += 1
        rows.append(
            ThresholdSlotCounts(
                threshold=threshold,
                exploration_count=exploration_count,
                diversity_count=diversity_count,
            )
        )
    return tuple(rows)


def summarize_novelty(scored: Sequence[ScoredCandidate], settings: ScoringSettings) -> NoveltyStats:
    """採点済み候補から novelty の分布と閾値走査をまとめる。"""
    values = tuple(item.breakdown.novelty for item in scored)
    distribution = summarize_novelty_distribution(
        values, exploration_min_novelty=settings.feed_composition.exploration_min_novelty
    )
    threshold_table = summarize_threshold_table(scored, settings)
    return NoveltyStats(distribution=distribution, threshold_table=threshold_table)
