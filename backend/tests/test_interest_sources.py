"""情報源単位の選好更新を検証する（`PROJECT_SPEC.md` §7.1 手順 4, §7.2, Issue #34）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import FeedbackAction
from techradar.interest.preferences import PreferenceDecaySettings
from techradar.interest.sources import (
    SourceWeights,
    apply_bad_feedback,
    compute_effective_weight,
    increase_positive_weight,
)
from techradar.recommendation.config import DEFAULT_CONFIG_PATH, load_scoring_config

GOOD = FeedbackAction.GOOD
BAD = FeedbackAction.BAD

SETTINGS = PreferenceDecaySettings(recent_window=5, bad_threshold=3, decay_step=1.0)


class TestIncreasePositiveWeight:
    def test_good_feedback_increases_positive_and_effective(self):
        # Arrange
        current = SourceWeights(positive=0.5, negative=0.0, effective=0.5)
        # Act
        updated = increase_positive_weight(current, increment=0.8)
        # Assert
        assert updated.positive == pytest.approx(1.3)
        assert updated.effective > current.effective

    def test_does_not_mutate_the_input(self):
        # Arrange
        current = SourceWeights(positive=0.5, negative=0.0, effective=0.5)
        # Act
        increase_positive_weight(current, increment=0.8)
        # Assert — immutable: 元の値は書き換わらない
        assert current.positive == 0.5


class TestComputeEffectiveWeight:
    """情報源の `effective_weight` は正負を打ち消し合う符号付きの値である。

    トピック側（`interest/topics.py`）の減衰係数方式とは異なる。Good された
    ことが一度も無い情報源でも、Bad が繰り返されれば抑制できる必要があるため
    （減衰係数方式だと positive=0 の情報源は negative がいくら増えても
    effective が 0 のまま動かない）。
    """

    def test_is_a_signed_subtraction(self):
        # Arrange / Act
        effective = compute_effective_weight(positive=1.0, negative=1.0)
        # Assert
        assert effective == pytest.approx(0.0)

    def test_goes_below_zero_when_only_bad_feedback_exists(self):
        # Arrange / Act — Good が一度も無い情報源でも抑制できる
        effective = compute_effective_weight(positive=0.0, negative=2.0)
        # Assert
        assert effective == pytest.approx(-2.0)

    def test_zero_negative_leaves_positive_unchanged(self):
        # Arrange / Act
        effective = compute_effective_weight(positive=0.7, negative=0.0)
        # Assert
        assert effective == pytest.approx(0.7)


class TestApplyBadFeedback:
    def test_does_not_change_weights_below_the_threshold(self):
        # Arrange — 単発（および閾値未満）の Bad では情報源選好を下げない
        current = SourceWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, GOOD, GOOD, GOOD, GOOD)
        # Act
        updated = apply_bad_feedback(current, recent, SETTINGS)
        # Assert
        assert updated == current

    def test_lowers_effective_weight_at_the_threshold(self):
        # Arrange — 同一情報源で Bad が繰り返された場合にのみ下がる
        current = SourceWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act
        updated = apply_bad_feedback(current, recent, SETTINGS)
        # Assert
        assert updated.negative == pytest.approx(1.0)
        assert updated.effective == pytest.approx(0.0)
        assert updated.effective < current.effective

    def test_is_consistent_with_repeated_application(self):
        """`negative_weight` は直近集合から毎回導出するため、二重に累積しない。"""
        # Arrange
        current = SourceWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, BAD, GOOD, GOOD)
        # Act
        once = apply_bad_feedback(current, recent, SETTINGS)
        twice = apply_bad_feedback(once, recent, SETTINGS)
        # Assert
        assert twice == once


class TestSourcePreferenceReflectsConfigChanges:
    """受入基準「増減量が設定ファイルで管理され、変更がテストに反映される」。"""

    def test_changing_decay_step_in_scoring_yaml_changes_the_penalty(self, tmp_path):
        # Arrange — 同梱の config/scoring.yaml の source_preference 側を差し替える
        original = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
        modified = original.replace("decay_step: 1.0", "decay_step: 2.0")
        assert modified != original
        path = tmp_path / "scoring.yaml"
        path.write_text(modified, encoding="utf-8")
        config = load_scoring_config(path)
        settings = PreferenceDecaySettings(
            recent_window=config.source_preference.recent_window,
            bad_threshold=config.source_preference.bad_threshold,
            decay_step=config.source_preference.decay_step,
        )
        current = SourceWeights(positive=1.0, negative=0.0, effective=1.0)
        recent = (BAD, BAD, BAD, GOOD, GOOD)

        # Act
        updated = apply_bad_feedback(current, recent, settings)

        # Assert — decay_step を 1.0 から 2.0 に変更した結果が反映される
        assert updated.negative == pytest.approx(2.0)
