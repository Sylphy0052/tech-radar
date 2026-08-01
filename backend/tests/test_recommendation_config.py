"""推薦スコアの設定ファイル読み込みを検証する（`PROJECT_SPEC.md` §14, §15）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from techradar.recommendation.config import (
    DEFAULT_CONFIG_PATH,
    ScoringConfig,
    ScoringConfigError,
    get_scoring_config,
    load_scoring_config,
)
from techradar.recommendation.ranking import (
    AuthorityGate,
    FeedComposition,
    FreshnessSettings,
    InterestSettings,
    MatchSettings,
    NoveltySettings,
    RankingLimits,
    ScorePenalties,
    ScoreWeights,
    ScoringSettings,
)

VALID_YAML = """\
weights:
  interest_similarity: 0.35
  source_authority: 0.30
  source_article_match: 0.10
  freshness: 0.10
  technical_quality: 0.10
  novelty: 0.05
penalties:
  bad: 1.0
  read: 0.3
authority_gate:
  min_interest_similarity: 0.35
  min_factor: 0.2
freshness:
  max_age_days: 7
interest:
  top_k: 3
source_match:
  partial_match_score: 0.5
novelty:
  default_when_no_topics: 0.5
feed_composition:
  strong_interest: 0.55
  primary_source: 0.25
  exploration: 0.15
  diversity: 0.05
  strong_interest_min_similarity: 0.5
  exploration_min_novelty: 0.6
limits:
  max_candidates_per_run: 500
  default_page_size: 20
  max_page_size: 100
"""


def write_config(tmp_path: Path, text: str) -> Path:
    """一時ディレクトリへ設定 YAML を書き出す。"""
    path = tmp_path / "scoring.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def config() -> ScoringConfig:
    """同梱の `config/scoring.yaml`。"""
    return load_scoring_config()


class TestLoading:
    def test_loads_the_bundled_config(self, config: ScoringConfig):
        # Arrange / Act / Assert — 同梱ファイルが常に読める（起動時に落ちない）
        assert DEFAULT_CONFIG_PATH.exists()
        assert config.weights.interest_similarity == 0.35
        assert config.weights.source_authority == 0.30

    def test_loads_the_bundled_authority_gate(self, config: ScoringConfig):
        # Arrange / Act / Assert
        assert config.authority_gate.min_interest_similarity == 0.35
        assert config.authority_gate.min_factor == 0.2

    def test_loads_the_bundled_feed_composition(self, config: ScoringConfig):
        # Arrange / Act / Assert — PROJECT_SPEC.md §15 の比率
        assert config.feed_composition.strong_interest == 0.55
        assert config.feed_composition.primary_source == 0.25
        assert config.feed_composition.exploration == 0.15
        assert config.feed_composition.diversity == 0.05

    def test_loads_the_bundled_limits(self, config: ScoringConfig):
        # Arrange / Act / Assert
        assert config.limits.default_page_size <= config.limits.max_page_size

    def test_rejects_weights_that_do_not_sum_to_one(self, tmp_path: Path):
        # Arrange
        broken = VALID_YAML.replace("interest_similarity: 0.35", "interest_similarity: 0.99")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="weights"):
            load_scoring_config(path)

    def test_rejects_a_feed_composition_that_does_not_sum_to_one(self, tmp_path: Path):
        # Arrange
        broken = VALID_YAML.replace("strong_interest: 0.55", "strong_interest: 0.99")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="feed_composition"):
            load_scoring_config(path)

    def test_rejects_a_weight_outside_the_valid_range(self, tmp_path: Path):
        # Arrange — 0.0〜1.0 の範囲外を起動時に弾く
        broken = VALID_YAML.replace("interest_similarity: 0.35", "interest_similarity: 1.5")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="interest_similarity"):
            load_scoring_config(path)

    def test_rejects_a_top_k_below_the_minimum(self, tmp_path: Path):
        # Arrange — 0 以下は平均を取れないため起動時に弾く
        broken = VALID_YAML.replace("top_k: 3", "top_k: 0")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="top_k"):
            load_scoring_config(path)

    def test_rejects_a_default_page_size_larger_than_the_maximum(self, tmp_path: Path):
        # Arrange — ページングの既定値が上限を超える設定は矛盾しているため弾く
        broken = VALID_YAML.replace("default_page_size: 20", "default_page_size: 200")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="default_page_size"):
            load_scoring_config(path)

    def test_rejects_an_unknown_key(self, tmp_path: Path):
        # Arrange — 設定漏れに気付けるよう未知のキーを許さない
        path = write_config(tmp_path, "weightz: {}\n")

        # Act / Assert
        with pytest.raises(ValueError, match="weightz"):
            load_scoring_config(path)

    def test_rejects_an_unknown_key_within_a_section(self, tmp_path: Path):
        # Arrange
        broken = VALID_YAML.replace(
            "interest_similarity: 0.35\n", "interest_similarity: 0.35\n  extra_key: 1.0\n"
        )
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="extra_key"):
            load_scoring_config(path)

    def test_reports_a_missing_file(self, tmp_path: Path):
        # Arrange / Act / Assert
        with pytest.raises(ScoringConfigError, match="読み込めません"):
            load_scoring_config(tmp_path / "absent.yaml")

    def test_reports_broken_yaml(self, tmp_path: Path):
        # Arrange
        path = write_config(tmp_path, "weights: [\n")

        # Act / Assert
        with pytest.raises(ScoringConfigError, match="YAML"):
            load_scoring_config(path)

    def test_reports_a_non_mapping_document(self, tmp_path: Path):
        # Arrange
        path = write_config(tmp_path, "- just\n- a list\n")

        # Act / Assert
        with pytest.raises(ScoringConfigError, match="マッピング"):
            load_scoring_config(path)

    def test_treats_an_empty_file_as_an_empty_mapping(self, tmp_path: Path):
        # Arrange — 空ファイルは yaml.safe_load が None を返すため、
        # 空マッピングとして扱った上で必須項目の不足として検証エラーになる
        path = write_config(tmp_path, "")

        # Act / Assert
        with pytest.raises(ValueError, match="weights"):
            load_scoring_config(path)

    def test_returns_the_bundled_singleton(self, config: ScoringConfig):
        # Arrange / Act
        cached = get_scoring_config()

        # Assert — 同梱ファイルをキャッシュ付きで読み込む
        assert cached == config


class TestConversionToRankingDataclasses:
    """`ranking.py` は Pydantic に依存させないため、専用の frozen dataclass へ変換する。"""

    def test_converts_to_scoring_settings(self, config: ScoringConfig):
        # Arrange / Act
        settings = config.to_settings()

        # Assert
        assert isinstance(settings, ScoringSettings)
        assert isinstance(settings.weights, ScoreWeights)
        assert isinstance(settings.penalties, ScorePenalties)
        assert isinstance(settings.authority_gate, AuthorityGate)
        assert isinstance(settings.freshness, FreshnessSettings)
        assert isinstance(settings.interest, InterestSettings)
        assert isinstance(settings.source_match, MatchSettings)
        assert isinstance(settings.novelty, NoveltySettings)
        assert isinstance(settings.feed_composition, FeedComposition)
        assert isinstance(settings.limits, RankingLimits)

    def test_preserves_values_through_the_conversion(self, config: ScoringConfig):
        # Arrange / Act
        settings = config.to_settings()

        # Assert
        assert settings.weights.interest_similarity == 0.35
        assert settings.penalties.bad == 1.0
        assert settings.authority_gate.min_factor == 0.2
        assert settings.freshness.max_age_days == 7
        assert settings.interest.top_k == 3
        assert settings.source_match.partial_match_score == 0.5
        assert settings.novelty.default_when_no_topics == 0.5
        assert settings.feed_composition.strong_interest == 0.55
        assert settings.limits.max_candidates_per_run == 500


class TestWeightsSumToOneAcrossConfig:
    def test_the_bundled_weights_sum_to_one(self, config: ScoringConfig):
        # Arrange / Act
        total = (
            config.weights.interest_similarity
            + config.weights.source_authority
            + config.weights.source_article_match
            + config.weights.freshness
            + config.weights.technical_quality
            + config.weights.novelty
        )

        # Assert
        assert total == pytest.approx(1.0)

    def test_the_bundled_feed_composition_sums_to_one(self, config: ScoringConfig):
        # Arrange / Act
        total = (
            config.feed_composition.strong_interest
            + config.feed_composition.primary_source
            + config.feed_composition.exploration
            + config.feed_composition.diversity
        )

        # Assert
        assert total == pytest.approx(1.0)
