"""フィード枠の充足率の集計（`techradar.measure.feed_slots`）のテスト（Issue #74）。

構成比 (55 / 25 / 15 / 5) の候補不足時にどの枠が縮退するかは未計測（Issue #73）。
`compose_feed_with_stats` は枠ごとの定員・実選択数・補充数を `CompositionStats` として
既に返すため、集計側では数え直さず、そこから充足率を導くだけにする。
"""

from __future__ import annotations

import pytest

from techradar.measure.feed_slots import FeedSlotStats, summarize_feed_slots
from techradar.recommendation.composition import (
    CompositionStats,
    FeedSlot,
    SlotStats,
)


def _stats(*slots: SlotStats) -> CompositionStats:
    return CompositionStats(slots=slots)


class TestSummarizeFeedSlots:
    def test_computes_fill_rate_per_slot(self) -> None:
        composition = _stats(
            SlotStats(slot=FeedSlot.STRONG_INTEREST, quota=11, selected=11, backfilled=0),
            SlotStats(slot=FeedSlot.PRIMARY_SOURCE, quota=5, selected=2, backfilled=0),
        )

        summary = summarize_feed_slots(composition, candidate_count=13, page_size=20)

        assert summary.candidate_count == 13
        assert summary.page_size == 20
        assert summary.slots == (
            FeedSlotStats(
                slot="strong_interest", quota=11, selected=11, backfilled=0, fill_rate=1.0
            ),
            FeedSlotStats(slot="primary_source", quota=5, selected=2, backfilled=0, fill_rate=0.4),
        )

    def test_fill_rate_is_zero_when_quota_is_zero(self) -> None:
        """定員 0 の枠は割り算できない。0 として扱い、例外にしない。"""
        composition = _stats(
            SlotStats(slot=FeedSlot.DIVERSITY, quota=0, selected=0, backfilled=0),
        )

        summary = summarize_feed_slots(composition, candidate_count=0, page_size=1)

        assert summary.slots[0].fill_rate == 0.0

    def test_reports_backfilled_count(self) -> None:
        """補充で埋まった件数を残す。どの枠が自力で埋まらなかったかを見るため。"""
        composition = _stats(
            SlotStats(slot=FeedSlot.EXPLORATION, quota=3, selected=3, backfilled=2),
        )

        summary = summarize_feed_slots(composition, candidate_count=10, page_size=20)

        assert summary.slots[0].backfilled == 2
        assert summary.slots[0].fill_rate == pytest.approx(1.0)

    def test_handles_empty_composition(self) -> None:
        """候補が 1 件も無い場合も落ちない。データが揃う前に実行されるため。"""
        summary = summarize_feed_slots(_stats(), candidate_count=0, page_size=20)

        assert summary.candidate_count == 0
        assert summary.slots == ()
