"""Discover フィードの構成比適用（`PROJECT_SPEC.md` §15）。

`ranking.py` の `rank_candidates` で採点・整列済みの候補群を、構成比の目安
（強い関心一致 55% / 一次情報枠 25% / 新規テーマ探索 15% / 多様性確保 5%）
に沿って混ぜ直す。判定は副作用を持たない純粋関数として実装する
（`PROJECT_SPEC.md` §25）。設定読み込みは `config.py` の責務のため、ここでは
import しない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from techradar.recommendation.ranking import ScoredCandidate, ScoringSettings


class FeedSlot(Enum):
    """候補が属するフィードの枠（`PROJECT_SPEC.md` §15）。"""

    STRONG_INTEREST = "strong_interest"
    PRIMARY_SOURCE = "primary_source"
    EXPLORATION = "exploration"
    DIVERSITY = "diversity"


# 枠の割当優先順位。先に該当した枠に入れる（`compose_feed_with_stats` の仕様）。
# 定員の丸め調整・枠選択・不足分の補充も、すべてこの順序で決定的に処理する。
_SLOT_PRIORITY: tuple[FeedSlot, ...] = (
    FeedSlot.STRONG_INTEREST,
    FeedSlot.PRIMARY_SOURCE,
    FeedSlot.EXPLORATION,
    FeedSlot.DIVERSITY,
)


@dataclass(frozen=True)
class SlotStats:
    """枠ごとの定員・実選択数・補充数。

    `selected` は最終的にこの枠へ割り当てられた候補数（枠自身の条件を満たして
    選ばれた分と、他の枠から補充された分の合計）。`backfilled` はそのうち
    補充によって埋まった件数の内数。
    """

    slot: FeedSlot
    quota: int
    selected: int
    backfilled: int


@dataclass(frozen=True)
class CompositionStats:
    """構成比適用の統計。`_SLOT_PRIORITY` の順に `SlotStats` を持つ。"""

    slots: tuple[SlotStats, ...]


@dataclass(frozen=True)
class ComposedFeed:
    """`compose_feed_with_stats` の戻り値。選択結果と統計を持つ。"""

    candidates: tuple[ScoredCandidate, ...]
    stats: CompositionStats


def _slot_for(scored: ScoredCandidate, settings: ScoringSettings) -> FeedSlot:
    """候補が属する枠を判定する。

    優先順位: 強い関心一致 → 一次情報・公式情報 → 新規テーマ探索 → 多様性確保。
    各候補は必ず 1 つの枠にだけ属する。
    """
    composition = settings.feed_composition
    if scored.breakdown.interest_similarity >= composition.strong_interest_min_similarity:
        return FeedSlot.STRONG_INTEREST
    if scored.candidate.is_primary_source:
        return FeedSlot.PRIMARY_SOURCE
    if scored.breakdown.novelty >= composition.exploration_min_novelty:
        return FeedSlot.EXPLORATION
    return FeedSlot.DIVERSITY


def _classify(
    scored: Sequence[ScoredCandidate], settings: ScoringSettings
) -> dict[FeedSlot, list[ScoredCandidate]]:
    """候補群を枠ごとのバケットへ分類する。入力の並び順（スコア降順）を保つ。"""
    buckets: dict[FeedSlot, list[ScoredCandidate]] = {slot: [] for slot in _SLOT_PRIORITY}
    for item in scored:
        buckets[_slot_for(item, settings)].append(item)
    return buckets


def _slot_quotas(page_size: int, settings: ScoringSettings) -> dict[FeedSlot, int]:
    """枠ごとの定員を計算する。

    比率 × page_size を四捨五入した値を基準にし、丸め誤差で合計が page_size と
    ずれる場合は、比率が大きい枠から順に 1 件ずつ加減して合計を一致させる
    （決定的な調整）。
    """
    composition = settings.feed_composition
    ratios: dict[FeedSlot, float] = {
        FeedSlot.STRONG_INTEREST: composition.strong_interest,
        FeedSlot.PRIMARY_SOURCE: composition.primary_source,
        FeedSlot.EXPLORATION: composition.exploration,
        FeedSlot.DIVERSITY: composition.diversity,
    }
    quotas = {slot: round(page_size * ratio) for slot, ratio in ratios.items()}
    order = sorted(_SLOT_PRIORITY, key=lambda slot: ratios[slot], reverse=True)

    diff = page_size - sum(quotas.values())
    step = 1 if diff > 0 else -1
    remaining = abs(diff)
    cursor = 0
    while remaining > 0:
        slot = order[cursor % len(order)]
        cursor += 1
        if step < 0 and quotas[slot] <= 0:
            continue
        quotas[slot] += step
        remaining -= 1
    return quotas


def _select_primary(
    buckets: dict[FeedSlot, list[ScoredCandidate]], quotas: dict[FeedSlot, int]
) -> dict[FeedSlot, list[ScoredCandidate]]:
    """各枠自身の条件を満たす候補から、定員までを選ぶ（補充より前の一次選択）。

    多様性枠だけは追加条件を課す。既に選ばれた候補（この一次選択の中で先に
    処理した枠を含む）と `source_domain` が重複しないものを優先して取り、
    重複しないものが尽きたらそれ以上は埋めない（不足分は補充で吸収する）。
    """
    selected: dict[FeedSlot, list[ScoredCandidate]] = {slot: [] for slot in _SLOT_PRIORITY}
    selected_domains: set[str] = set()

    for slot in _SLOT_PRIORITY:
        quota = quotas[slot]
        bucket = buckets[slot]
        if slot is FeedSlot.DIVERSITY:
            for item in bucket:
                if len(selected[slot]) >= quota:
                    break
                domain = item.candidate.source_domain
                if domain in selected_domains:
                    continue
                selected[slot].append(item)
                selected_domains.add(domain)
        else:
            for item in bucket[:quota]:
                selected[slot].append(item)
                selected_domains.add(item.candidate.source_domain)

    return selected


def compose_feed_with_stats(
    scored: Sequence[ScoredCandidate], settings: ScoringSettings, page_size: int
) -> ComposedFeed:
    """構成比に沿って候補を選び、選択結果と枠ごとの統計を返す。

    `scored` は `rank_candidates` の出力のようにスコア降順で整列済みである
    ことを前提とし、この関数自身は並べ替えない（枠内の優先順位判定に使う）。
    ただし最終的な出力（`ComposedFeed.candidates`）は、構成比で選んだ候補を
    スコア降順（同点は id 昇順）へ改めて並べ直したものになる。フィードの
    表示順はスコア順とし、構成比は「どの候補を選ぶか」にだけ効く。

    候補が定員に満たない枠は、他の枠の未選択候補からスコア降順で補充する。
    候補総数が `page_size` に満たない場合は、ある分だけを返し例外にしない。
    同じ候補が 2 回選ばれることはない。
    """
    quotas = _slot_quotas(page_size, settings)
    if not scored:
        empty_stats = CompositionStats(
            slots=tuple(
                SlotStats(slot=slot, quota=quotas[slot], selected=0, backfilled=0)
                for slot in _SLOT_PRIORITY
            )
        )
        return ComposedFeed(candidates=(), stats=empty_stats)

    buckets = _classify(scored, settings)
    selected = _select_primary(buckets, quotas)

    selected_ids = {item.candidate.id for items in selected.values() for item in items}
    leftover = [item for item in scored if item.candidate.id not in selected_ids]

    backfilled_counts: dict[FeedSlot, int] = dict.fromkeys(_SLOT_PRIORITY, 0)
    leftover_index = 0
    for slot in _SLOT_PRIORITY:
        shortfall = quotas[slot] - len(selected[slot])
        while shortfall > 0 and leftover_index < len(leftover):
            candidate = leftover[leftover_index]
            leftover_index += 1
            selected[slot].append(candidate)
            backfilled_counts[slot] += 1
            shortfall -= 1

    all_selected = [item for items in selected.values() for item in items]
    ordered = tuple(
        sorted(all_selected, key=lambda item: (-item.breakdown.total, str(item.candidate.id)))
    )

    stats = CompositionStats(
        slots=tuple(
            SlotStats(
                slot=slot,
                quota=quotas[slot],
                selected=len(selected[slot]),
                backfilled=backfilled_counts[slot],
            )
            for slot in _SLOT_PRIORITY
        )
    )
    return ComposedFeed(candidates=ordered, stats=stats)


def compose_feed(
    scored: Sequence[ScoredCandidate], settings: ScoringSettings, page_size: int
) -> tuple[ScoredCandidate, ...]:
    """構成比に沿って候補を選ぶ（`compose_feed_with_stats` の薄いラッパー）。"""
    return compose_feed_with_stats(scored, settings, page_size).candidates
