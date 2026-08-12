"""フィード枠の充足率の集計（Issue #74、Issue #73 の前提）。

構成比 (55 / 25 / 15 / 5) は定義済みだが、実際の候補分布でどの枠が定員を満たせず、
どこから補充されているかは未計測。`compose_feed_with_stats` が返す `CompositionStats`
（枠ごとの定員・実選択数・補充数）から充足率を導く。

枠の判定や補充の規則はここでは持たない。`recommendation.composition` の結果をそのまま
読むことで、測っている対象を本番の挙動と一致させる。
"""

from __future__ import annotations

from dataclasses import dataclass

from techradar.recommendation.composition import CompositionStats


@dataclass(frozen=True)
class FeedSlotStats:
    """1 枠分の集計。`fill_rate` は定員に対する実選択数の割合。"""

    slot: str
    quota: int
    selected: int
    backfilled: int
    fill_rate: float


@dataclass(frozen=True)
class FeedCompositionStats:
    """フィード構成の集計。`candidate_count` は採点済み候補の総数。"""

    candidate_count: int
    page_size: int
    slots: tuple[FeedSlotStats, ...]


def summarize_feed_slots(
    composition: CompositionStats, *, candidate_count: int, page_size: int
) -> FeedCompositionStats:
    """枠ごとの充足率を求める。

    定員 0 の枠は割り算ができないため充足率を 0 とする。ページ件数が小さいと
    比率の低い枠の定員が 0 に丸まるため、例外にせず値で表す。
    """
    return FeedCompositionStats(
        candidate_count=candidate_count,
        page_size=page_size,
        slots=tuple(
            FeedSlotStats(
                slot=slot.slot.value,
                quota=slot.quota,
                selected=slot.selected,
                backfilled=slot.backfilled,
                fill_rate=(slot.selected / slot.quota) if slot.quota > 0 else 0.0,
            )
            for slot in composition.slots
        ),
    )
