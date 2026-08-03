"""関心の実効重み計算を検証する（`PROJECT_SPEC.md` §7.1, §8）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import ArticleOrigin
from techradar.interest.weights import (
    MAX_CONFIDENCE,
    ConfidenceSettings,
    FeedbackWeights,
    compute_confidence,
    compute_effective_interest,
    compute_recency_decay,
    explicit_weight_for_origin,
)
from techradar.recommendation.config import DEFAULT_CONFIG_PATH, load_scoring_config

WEIGHTS = FeedbackWeights(manual=1.0, good=0.8, save=0.5, read_full=0.2, clicked=0.1, bad=0.8)
CONFIDENCE_SETTINGS = ConfidenceSettings(
    has_embedding=0.4, has_topics=0.3, is_analyzed=0.3, min_confidence=0.3
)


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
            confidence=MAX_CONFIDENCE,
        )
        old = compute_effective_interest(
            explicit_weight=1.0,
            feedback_weight=0.8,
            recency_decay=old_decay,
            confidence=MAX_CONFIDENCE,
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

    def test_lower_confidence_lowers_the_effective_interest(self):
        # Arrange / Act — 受入基準「effective_interest の計算に confidence が反映される」
        certain = compute_effective_interest(
            explicit_weight=1.0, feedback_weight=1.0, recency_decay=1.0, confidence=1.0
        )
        uncertain = compute_effective_interest(
            explicit_weight=1.0, feedback_weight=1.0, recency_decay=1.0, confidence=0.3
        )
        # Assert
        assert uncertain < certain


class TestComputeConfidence:
    """記事のシグナル充足度から確信度を導く（`PROJECT_SPEC.md` §8、Issue #20）。"""

    def test_is_one_when_every_signal_is_present(self):
        # Arrange / Act
        confidence = compute_confidence(
            has_embedding=True, has_topics=True, is_analyzed=True, settings=CONFIDENCE_SETTINGS
        )
        # Assert
        assert confidence == pytest.approx(MAX_CONFIDENCE)

    def test_falls_back_to_the_minimum_when_no_signal_is_present(self):
        # Arrange / Act — クリックされただけで解析前の記事
        confidence = compute_confidence(
            has_embedding=False, has_topics=False, is_analyzed=False, settings=CONFIDENCE_SETTINGS
        )
        # Assert — 寄与をゼロにはしない（関心記事であること自体は事実のため）
        assert confidence == pytest.approx(CONFIDENCE_SETTINGS.min_confidence)

    def test_a_missing_signal_lowers_the_confidence(self):
        # Arrange / Act
        full = compute_confidence(
            has_embedding=True, has_topics=True, is_analyzed=True, settings=CONFIDENCE_SETTINGS
        )
        without_embedding = compute_confidence(
            has_embedding=False, has_topics=True, is_analyzed=True, settings=CONFIDENCE_SETTINGS
        )
        # Assert
        assert without_embedding < full
        assert without_embedding == pytest.approx(
            CONFIDENCE_SETTINGS.has_topics + CONFIDENCE_SETTINGS.is_analyzed
        )

    def test_sums_only_the_satisfied_signals(self):
        # Arrange / Act
        confidence = compute_confidence(
            has_embedding=True, has_topics=False, is_analyzed=True, settings=CONFIDENCE_SETTINGS
        )
        # Assert
        assert confidence == pytest.approx(
            CONFIDENCE_SETTINGS.has_embedding + CONFIDENCE_SETTINGS.is_analyzed
        )

    def test_never_exceeds_the_maximum(self):
        # Arrange — 設定の合計が 1.0 を超えても係数として 1.0 を超えさせない
        settings = ConfidenceSettings(
            has_embedding=0.8, has_topics=0.8, is_analyzed=0.8, min_confidence=0.3
        )
        # Act
        confidence = compute_confidence(
            has_embedding=True, has_topics=True, is_analyzed=True, settings=settings
        )
        # Assert
        assert confidence == pytest.approx(MAX_CONFIDENCE)


class TestConfidenceReflectsConfigChanges:
    """受入基準「各信号の寄与が設定ファイルで管理される」。"""

    def test_changing_min_confidence_in_scoring_yaml_changes_the_floor(self, tmp_path):
        # Arrange
        original = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
        modified = original.replace("min_confidence: 0.3", "min_confidence: 0.6")
        assert modified != original
        path = tmp_path / "scoring.yaml"
        path.write_text(modified, encoding="utf-8")
        config = load_scoring_config(path)
        settings = ConfidenceSettings(
            has_embedding=config.confidence.has_embedding,
            has_topics=config.confidence.has_topics,
            is_analyzed=config.confidence.is_analyzed,
            min_confidence=config.confidence.min_confidence,
        )

        # Act
        confidence = compute_confidence(
            has_embedding=False, has_topics=False, is_analyzed=False, settings=settings
        )

        # Assert
        assert confidence == pytest.approx(0.6)


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
