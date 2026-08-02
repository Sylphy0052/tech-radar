"""関心の実効重み計算を検証する（`PROJECT_SPEC.md` §7.1, §8）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import ArticleOrigin
from techradar.interest.weights import (
    DEFAULT_CONFIDENCE,
    FeedbackWeights,
    compute_effective_interest,
    compute_recency_decay,
    explicit_weight_for_origin,
)

WEIGHTS = FeedbackWeights(manual=1.0, good=0.8, save=0.5, read_full=0.2, clicked=0.1, bad=0.8)


class TestComputeRecencyDecay:
    def test_is_one_at_zero_days(self):
        # Arrange / Act
        decay = compute_recency_decay(age_days=0.0, half_life_days=30.0)
        # Assert
        assert decay == pytest.approx(1.0)

    def test_is_one_half_at_the_half_life(self):
        # Arrange / Act
        decay = compute_recency_decay(age_days=30.0, half_life_days=30.0)
        # Assert
        assert decay == pytest.approx(0.5)

    def test_decreases_monotonically_as_age_increases(self):
        # Arrange
        ages = (0.0, 10.0, 30.0, 60.0, 90.0)
        # Act
        decays = [compute_recency_decay(age_days=age, half_life_days=30.0) for age in ages]
        # Assert
        assert decays == sorted(decays, reverse=True)
        assert len(set(decays)) == len(decays)

    def test_clamps_future_timestamps_to_one(self):
        # Arrange / Act — クロックずれ等で未来日時（負の経過日数）になった場合
        decay = compute_recency_decay(age_days=-5.0, half_life_days=30.0)
        # Assert
        assert decay == pytest.approx(1.0)


class TestComputeEffectiveInterest:
    def test_older_interest_has_smaller_effective_interest(self):
        # Arrange — 受入基準「時間減衰により古い関心のeffective_interestが単調減少する」
        recent_decay = compute_recency_decay(age_days=1.0, half_life_days=30.0)
        old_decay = compute_recency_decay(age_days=60.0, half_life_days=30.0)

        # Act
        recent = compute_effective_interest(
            explicit_weight=1.0,
            feedback_weight=0.8,
            recency_decay=recent_decay,
            confidence=DEFAULT_CONFIDENCE,
        )
        old = compute_effective_interest(
            explicit_weight=1.0,
            feedback_weight=0.8,
            recency_decay=old_decay,
            confidence=DEFAULT_CONFIDENCE,
        )

        # Assert
        assert old < recent

    def test_multiplies_all_four_factors(self):
        # Arrange / Act
        value = compute_effective_interest(
            explicit_weight=0.5, feedback_weight=0.8, recency_decay=0.25, confidence=0.9
        )
        # Assert
        assert value == pytest.approx(0.5 * 0.8 * 0.25 * 0.9)

    def test_default_confidence_is_one(self):
        # Arrange / Act / Assert — Issue #20 実装までは常にこの値を使う
        assert DEFAULT_CONFIDENCE == 1.0


class TestExplicitWeightForOrigin:
    @pytest.mark.parametrize(
        ("origin", "expected"),
        [
            (ArticleOrigin.MANUAL, 1.0),
            (ArticleOrigin.GOOD, 0.8),
            (ArticleOrigin.SAVED, 0.5),
            (ArticleOrigin.READ_FULL, 0.2),
            (ArticleOrigin.CLICKED, 0.1),
        ],
    )
    def test_resolves_the_weight_matching_the_origin(self, origin: ArticleOrigin, expected: float):
        # Arrange / Act
        weight = explicit_weight_for_origin(origin, WEIGHTS)
        # Assert
        assert weight == pytest.approx(expected)
