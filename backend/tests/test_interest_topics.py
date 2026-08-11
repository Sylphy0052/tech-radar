"""トピック単位の選好更新を検証する（`PROJECT_SPEC.md` §7.1, §7.2）。

判定は純粋関数として実装するため、DB を使わずに検証できる。トピックと情報源
（`interest/sources.py`）で共有する Bad 判定そのものの検証は
`test_interest_preferences.py` が担う。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import FeedbackAction
from techradar.interest.preferences import PreferenceDecaySettings, compute_negative_weight
from techradar.interest.topics import (
    TopicWeights,
    apply_bad_feedback,
    compute_effective_weight,
    increase_positive_weight,
)
from techradar.recommendation.config import DEFAULT_CONFIG_PATH, load_scoring_config

GOOD = FeedbackAction.GOOD
BAD = FeedbackAction.BAD

SETTINGS = PreferenceDecaySettings(recent_window=5, bad_threshold=3, decay_step=0.2)


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
    """トピック側の増加方向（`apply_bad_feedback`）が共有の導出関数と一致することを検証する。

    `negative_weight` を「これまでの増分の累積」ではなく「今の直近集合が示す値」
    として導出すること自体の検証は `test_interest_preferences.py` が担う。ここで
    見るのは、その共有関数と取り消し後の再計算（`interest/service.py` の
    `recompute_topic_preferences_after_removal`）が食い違わない前提（Issue #15
    自己レビュー 1）だけ。
    """

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
        settings = PreferenceDecaySettings(
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
