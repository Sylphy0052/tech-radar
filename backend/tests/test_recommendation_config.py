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
    BadSimilaritySettings,
    FeedComposition,
    FreshnessSettings,
    InterestSettings,
    MatchSettings,
    NoveltySettings,
    RankingLimits,
    ScorePenalties,
    ScoreWeights,
    ScoringSettings,
    SourcePreferenceGate,
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
  max_profile_articles: 200
  max_bad_profile_articles: 100
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
  feed_run_size: 100
  article_based_run_size: 20
  feed_run_reuse_seconds: 600
feedback_weights:
  manual: 1.0
  good: 0.8
  save: 0.5
  read_full: 0.2
  clicked: 0.1
  bad: 0.8
interest_decay:
  half_life_days: 30
confidence:
  has_embedding: 0.4
  has_topics: 0.3
  is_analyzed: 0.3
  min_confidence: 0.3
topic_preference:
  recent_window: 5
  bad_threshold: 3
  decay_step: 0.2
source_preference:
  recent_window: 5
  bad_threshold: 3
  decay_step: 1.0
  weight_scale: 0.15
  min_factor: 0.5
  max_factor: 1.5
bad_similarity:
  min_similarity: 0.7
  max_penalty: 0.5
clustering:
  min_clusters: 2
  max_clusters: 8
  min_articles_per_cluster: 3
  label_topic_count: 3
  random_state: 0
interest_timeline:
  default_weeks: 12
  max_weeks: 52
interest_summary:
  max_genres: 20
  max_technologies: 20
  max_suppressed_topics: 20
  max_content_types: 20
  max_difficulties: 20
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

    def test_loads_the_bundled_max_bad_profile_articles(self, config: ScoringConfig):
        # Arrange / Act / Assert — Bad 近傍抑制用の embedding 数上限（Issue #15 段階 2）
        assert config.interest.max_bad_profile_articles > 0

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

    def test_loads_the_bundled_feedback_weights(self, config: ScoringConfig):
        # Arrange / Act / Assert — PROJECT_SPEC.md §7.1, §7.2 の重み表
        assert config.feedback_weights.manual == 1.0
        assert config.feedback_weights.good == 0.8
        assert config.feedback_weights.save == 0.5
        assert config.feedback_weights.read_full == 0.2
        assert config.feedback_weights.clicked == 0.1
        assert config.feedback_weights.bad == 0.8

    def test_loads_the_bundled_interest_decay(self, config: ScoringConfig):
        # Arrange / Act / Assert
        assert config.interest_decay.half_life_days == 30

    def test_loads_the_bundled_topic_preference(self, config: ScoringConfig):
        # Arrange / Act / Assert — PROJECT_SPEC.md §7.2 の例（直近5件中3件以上）
        assert config.topic_preference.recent_window == 5
        assert config.topic_preference.bad_threshold == 3
        assert config.topic_preference.decay_step == 0.2

    def test_loads_the_bundled_confidence(self, config: ScoringConfig):
        # Arrange / Act / Assert — 記事シグナルの充足度（Issue #20）
        assert config.confidence.has_embedding == 0.4
        assert config.confidence.has_topics == 0.3
        assert config.confidence.is_analyzed == 0.3
        assert config.confidence.min_confidence == 0.3

    def test_rejects_confidence_signals_that_do_not_sum_to_one(self, tmp_path: Path):
        # Arrange — 全シグナルが揃った記事の confidence が 1.0 にならない設定は弾く
        broken = VALID_YAML.replace("has_embedding: 0.4", "has_embedding: 0.9")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="confidence"):
            load_scoring_config(path)

    def test_rejects_a_min_confidence_above_the_smallest_signal(self, tmp_path: Path):
        # Arrange — 下限が個々のシグナルより大きいと、シグナル 1 つの記事と
        # 0 個の記事の confidence が同じ値に潰れる
        broken = VALID_YAML.replace("min_confidence: 0.3", "min_confidence: 0.35")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="min_confidence"):
            load_scoring_config(path)

    def test_loads_the_bundled_source_preference(self, config: ScoringConfig):
        # Arrange / Act / Assert — 情報源選好は専用セクションで管理する（Issue #34）
        assert config.source_preference.recent_window == 5
        assert config.source_preference.bad_threshold == 3
        assert config.source_preference.decay_step == 1.0
        assert config.source_preference.weight_scale == 0.15
        assert config.source_preference.min_factor == 0.5
        assert config.source_preference.max_factor == 1.5

    def test_rejects_a_source_preference_min_factor_larger_than_the_max_factor(
        self, tmp_path: Path
    ):
        # Arrange — 係数の下限が上限を超える設定は clamp が成立しない
        broken = VALID_YAML.replace("min_factor: 0.5", "min_factor: 2.0")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="min_factor"):
            load_scoring_config(path)

    def test_rejects_a_source_preference_range_that_excludes_the_neutral_factor(
        self, tmp_path: Path
    ):
        # Arrange — 範囲が 1.0 を含まないと、選好が無い情報源にも係数が掛かり
        # 「学習前は従来と同じスコア」という前提が崩れる
        broken = VALID_YAML.replace("min_factor: 0.5", "min_factor: 1.1")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match=r"1\.0"):
            load_scoring_config(path)

    def test_loads_the_bundled_bad_similarity(self, config: ScoringConfig):
        # Arrange / Act / Assert
        assert config.bad_similarity.min_similarity == 0.7
        assert config.bad_similarity.max_penalty == 0.5

    def test_loads_the_bundled_clustering(self, config: ScoringConfig):
        # Arrange / Act / Assert
        assert config.clustering.min_clusters <= config.clustering.max_clusters
        assert config.clustering.min_articles_per_cluster == 3
        assert config.clustering.label_topic_count == 3
        assert config.clustering.random_state == 0

    def test_rejects_a_bad_threshold_larger_than_the_recent_window(self, tmp_path: Path):
        # Arrange — 「一件のBadだけでジャンル全体を抑制しない」判定が
        # recent_window 件を超えて Bad を数えることは矛盾しているため弾く
        broken = VALID_YAML.replace("bad_threshold: 3", "bad_threshold: 6")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="bad_threshold"):
            load_scoring_config(path)

    def test_rejects_a_min_clusters_larger_than_max_clusters(self, tmp_path: Path):
        # Arrange
        broken = VALID_YAML.replace("min_clusters: 2", "min_clusters: 9")
        path = write_config(tmp_path, broken)

        # Act / Assert
        with pytest.raises(ValueError, match="min_clusters"):
            load_scoring_config(path)


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
        assert isinstance(settings.bad_similarity, BadSimilaritySettings)
        assert isinstance(settings.source_preference, SourcePreferenceGate)

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
        assert settings.bad_similarity.min_similarity == 0.7
        assert settings.bad_similarity.max_penalty == 0.5
        assert settings.source_preference.weight_scale == 0.15
        assert settings.source_preference.min_factor == 0.5
        assert settings.source_preference.max_factor == 1.5


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
