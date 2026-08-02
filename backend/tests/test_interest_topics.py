"""トピック単位の選好更新を検証する（`PROJECT_SPEC.md` §7.1, §7.2）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import FeedbackAction
from techradar.interest.topics import (
    TopicPreferenceSettings,
    TopicWeights,
    apply_bad_feedback,
    compute_effective_weight,
    compute_negative_weight,
    increase_positive_weight,
    should_penalize_topic,
)
from techradar.recommendation.config import DEFAULT_CONFIG_PATH, load_scoring_config

GOOD = FeedbackAction.GOOD
BAD = FeedbackAction.BAD

SETTINGS = TopicPreferenceSettings(recent_window=5, bad_threshold=3, decay_step=0.2)


class TestShouldPenalizeTopic:
    def test_does_not_penalize_when_one_of_five_recent_is_bad(self):
        # Arrange
        recent = (BAD, GOOD, GOOD, GOOD, GOOD)
        # Act / Assert — 直近5記事中1記事がBad
        assert should_penalize_topic(recent, recent_window=5, bad_threshold=3) is False

    def test_does_not_penalize_when_two_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, GOOD, GOOD, GOOD)
        # Act / Assert — 直近5記事中2記事がBad
        assert should_penalize_topic(recent, recent_window=5, bad_threshold=3) is False

    def test_penalizes_when_three_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act / Assert — 直近5記事中3記事以上がBad（PROJECT_SPEC.md §7.2 の例）
        assert should_penalize_topic(recent, recent_window=5, bad_threshold=3) is True

    def test_penalizes_when_four_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, BAD, BAD, GOOD)
        # Act / Assert
        assert should_penalize_topic(recent, recent_window=5, bad_threshold=3) is True

    def test_penalizes_when_five_of_five_recent_are_bad(self):
        # Arrange
        recent = (BAD, BAD, BAD, BAD, BAD)
        # Act / Assert
        assert should_penalize_topic(recent, recent_window=5, bad_threshold=3) is True

    def test_ignores_bad_feedback_outside_the_recent_window(self):
        # Arrange — recent_window より古い（先頭から6件目以降の）Bad は数えない
        recent = (GOOD, GOOD, GOOD, GOOD, GOOD, BAD, BAD, BAD)
        # Act / Assert
        assert should_penalize_topic(recent, recent_window=5, bad_threshold=3) is False


class TestIncreasePositiveWeight:
    def test_good_feedback_increases_positive_and_effective(self):
        # Arrange
        current = TopicWeights(positive=0.5, negative=0.0, effective=0.5)
        # Act
        updated = increase_positive_weight(current, increment=0.8)
        # Assert
        assert updated.positive == pytest.approx(1.3)
        assert updated.effective > current.effective

    def test_does_not_mutate_the_input(self):
        # Arrange
        current = TopicWeights(positive=0.5, negative=0.0, effective=0.5)
        # Act
        increase_positive_weight(current, increment=0.8)
        # Assert — immutable: 元の値は書き換わらない
        assert current.positive == 0.5


class TestComputeEffectiveWeight:
    def test_is_not_a_simple_subtraction(self):
        # Arrange / Act
        effective = compute_effective_weight(positive=1.0, negative=1.0)
        # Assert — 単純な positive - negative（この場合 0.0）にはしない
        assert effective != 1.0 - 1.0
        assert effective > 0.0

    def test_negative_never_pushes_effective_below_zero(self):
        # Arrange / Act
        effective = compute_effective_weight(positive=1.0, negative=10.0)
        # Assert
        assert effective > 0.0

    def test_zero_negative_leaves_positive_unchanged(self):
        # Arrange / Act
        effective = compute_effective_weight(positive=0.7, negative=0.0)
        # Assert
        assert effective == pytest.approx(0.7)


class TestApplyBadFeedback:
    def test_does_not_change_weights_below_the_threshold(self):
        # Arrange
        current = TopicWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, GOOD, GOOD, GOOD)
        # Act
        updated = apply_bad_feedback(current, recent, SETTINGS)
        # Assert
        assert updated == current

    def test_increases_negative_by_decay_step_at_the_threshold(self):
        # Arrange
        current = TopicWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act
        updated = apply_bad_feedback(current, recent, SETTINGS)
        # Assert
        assert updated.negative == pytest.approx(0.2)
        assert updated.effective < current.effective


class TestComputeNegativeWeight:
    """`negative_weight` を直近フィードバック集合から一意に導出することを検証する。

    「これまでの増分の累積」ではなく「今の直近集合が示す値」であることが、
    フィードバック取り消し後の再計算（`interest/service.py` の
    `recompute_topic_preferences_after_removal`）と整合する前提（Issue #15
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

    def test_is_consistent_with_apply_bad_feedback(self):
        """`apply_bad_feedback`（増加方向）と同じ値を返すことを固定する。"""
        # Arrange
        current = TopicWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act
        updated = apply_bad_feedback(current, recent, SETTINGS)
        recomputed = compute_negative_weight(recent, SETTINGS)
        # Assert
        assert updated.negative == pytest.approx(recomputed)


class TestTopicPreferenceReflectsConfigChanges:
    """Issue の受入基準「重み定数が設定ファイルで管理され、変更がテストに反映される」。"""

    def test_changing_decay_step_in_scoring_yaml_changes_the_penalty(self, tmp_path):
        # Arrange — 同梱の config/scoring.yaml を差し替えて読み込む
        original = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
        modified = original.replace("decay_step: 0.2", "decay_step: 0.5")
        assert modified != original
        path = tmp_path / "scoring.yaml"
        path.write_text(modified, encoding="utf-8")
        config = load_scoring_config(path)
        settings = TopicPreferenceSettings(
            recent_window=config.topic_preference.recent_window,
            bad_threshold=config.topic_preference.bad_threshold,
            decay_step=config.topic_preference.decay_step,
        )
        current = TopicWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, BAD, GOOD, GOOD)

        # Act
        updated = apply_bad_feedback(current, recent, settings)

        # Assert — decay_step を 0.2 から 0.5 に変更した結果が反映される
        assert updated.negative == pytest.approx(0.5)
