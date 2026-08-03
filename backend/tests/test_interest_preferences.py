"""選好更新に共通する Bad 判定を検証する（`PROJECT_SPEC.md` §7.2）。

トピック単位（`interest/topics.py`）と情報源単位（`interest/sources.py`）で共有する
「直近 N 件中 Bad が M 件以上のときだけ段階的に下げる」判定の検証。判定は純粋関数
として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import FeedbackAction
from techradar.interest.preferences import (
    PreferenceDecaySettings,
    compute_negative_weight,
    should_penalize,
)

GOOD = FeedbackAction.GOOD
BAD = FeedbackAction.BAD

SETTINGS = PreferenceDecaySettings(recent_window=5, bad_threshold=3, decay_step=0.2)


class TestShouldPenalize:
    def test_does_not_penalize_when_one_of_five_recent_is_bad(self):
        # Arrange
        recent = (BAD, GOOD, GOOD, GOOD, GOOD)
        # Act / Assert — 直近5記事中1記事がBad
        assert should_penalize(recent, recent_window=5, bad_threshold=3) is False

    def test_does_not_penalize_when_two_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, GOOD, GOOD, GOOD)
        # Act / Assert — 直近5記事中2記事がBad
        assert should_penalize(recent, recent_window=5, bad_threshold=3) is False

    def test_penalizes_when_three_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act / Assert — 直近5記事中3記事以上がBad（PROJECT_SPEC.md §7.2 の例）
        assert should_penalize(recent, recent_window=5, bad_threshold=3) is True

    def test_penalizes_when_four_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, BAD, BAD, GOOD)
        # Act / Assert
        assert should_penalize(recent, recent_window=5, bad_threshold=3) is True

    def test_penalizes_when_five_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, BAD, BAD, BAD)
        # Act / Assert
        assert should_penalize(recent, recent_window=5, bad_threshold=3) is True

    def test_ignores_bad_feedback_outside_the_recent_window(self):
        # Arrange — recent_window より古い（先頭から6件目以降の）Bad は数えない
        recent = (GOOD, GOOD, GOOD, GOOD, GOOD, BAD, BAD, BAD)
        # Act / Assert
        assert should_penalize(recent, recent_window=5, bad_threshold=3) is False


class TestComputeNegativeWeight:
    """`negative_weight` を直近フィードバック集合から一意に導出することを検証する。

    「これまでの増分の累積」ではなく「今の直近集合が示す値」であることが、
    フィードバック取り消し後の再計算（`interest/service.py` の
    `recompute_topic_preferences_after_removal` /
    `recompute_source_preferences_after_removal`）と整合する前提（Issue #15
    自己レビュー 1）。
    """

    def test_returns_zero_when_below_the_threshold(self):
        # Arrange
        recent = (BAD, BAD, GOOD, GOOD, GOOD)
        # Act / Assert — 直近5件中2件のBadでは抑制しない
        assert compute_negative_weight(recent, SETTINGS) == pytest.approx(0.0)

    def test_returns_one_step_at_the_exact_threshold(self):
        # Arrange
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act / Assert — ちょうど閾値（3/5）で decay_step 1 段階分
        assert compute_negative_weight(recent, SETTINGS) == pytest.approx(0.2)

    def test_returns_more_steps_when_more_bad_than_the_threshold(self):
        # Arrange
        recent = (BAD, BAD, BAD, BAD, BAD)
        # Act / Assert — 5/5 が Bad（閾値を2件超過）で decay_step 3 段階分
        assert compute_negative_weight(recent, SETTINGS) == pytest.approx(0.6)

    def test_is_not_cumulative_across_calls(self):
        """状態が呼び出し回数ではなく「今の直近集合」だけから定まることを固定する。"""
        # Arrange
        below_threshold = (BAD, BAD, GOOD, GOOD, GOOD)
        at_threshold = (BAD, BAD, BAD, GOOD, GOOD)
        # Act — 同じ入力なら、直前に別の入力で何度呼んでいても結果は変わらない
        compute_negative_weight(at_threshold, SETTINGS)
        compute_negative_weight(at_threshold, SETTINGS)
        result = compute_negative_weight(below_threshold, SETTINGS)
        # Assert
        assert result == pytest.approx(0.0)
