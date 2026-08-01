"""重複判定の設定ファイル読み込みを検証する（`PROJECT_SPEC.md` §17, §24）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from techradar.db.enums import ContentType
from techradar.dedup.config import (
    DEFAULT_CONFIG_PATH,
    DedupConfig,
    DedupConfigError,
    load_dedup_config,
)
from techradar.dedup.rules import DuplicatePenalties, DuplicateThresholds, UniqueValueSettings


@pytest.fixture(scope="module")
def config() -> DedupConfig:
    """同梱の `config/dedup.yaml`。"""
    return load_dedup_config()


class TestLoading:
    def test_loads_the_bundled_config(self, config: DedupConfig):
        # Arrange / Act / Assert — 同梱ファイルが常に読める（起動時に落ちない）
        assert DEFAULT_CONFIG_PATH.exists()
        assert config.thresholds.title_similarity == 0.90
        assert config.thresholds.embedding_similarity == 0.92

    def test_loads_the_bundled_penalties(self, config: DedupConfig):
        # Arrange / Act / Assert
        assert config.penalties.canonical_url == 1.0
        assert config.penalties.normalized_url == 1.0
        assert config.penalties.body_hash == 1.0
        assert config.penalties.title == 0.8
        assert config.penalties.embedding == 0.6

    def test_loads_the_bundled_unique_value_settings(self, config: DedupConfig):
        # Arrange / Act / Assert
        assert set(config.unique_value.content_types) == {
            ContentType.IMPLEMENTATION,
            ContentType.RESEARCH,
        }
        assert config.unique_value.min_technical_quality == 0.70
        assert config.unique_value.max_authority_gap == 0.30
        assert config.unique_value.max_candidates_per_cluster == 2

    def test_rejects_a_threshold_outside_the_valid_range(self, tmp_path: Path):
        # Arrange — 0.0〜1.0 の範囲外を起動時に弾く
        path = tmp_path / "dedup.yaml"
        path.write_text(
            "thresholds:\n"
            "  title_similarity: 1.5\n"
            "  embedding_similarity: 0.9\n"
            "penalties:\n"
            "  canonical_url: 1.0\n"
            "  normalized_url: 1.0\n"
            "  body_hash: 1.0\n"
            "  title: 0.8\n"
            "  embedding: 0.6\n"
            "unique_value:\n"
            "  content_types: [implementation]\n"
            "  min_technical_quality: 0.7\n"
            "  max_authority_gap: 0.3\n"
            "  max_candidates_per_cluster: 2\n",
            encoding="utf-8",
        )

        # Act / Assert
        with pytest.raises(ValueError, match="title_similarity"):
            load_dedup_config(path)

    def test_rejects_an_unknown_content_type(self, tmp_path: Path):
        # Arrange — 種別の打ち間違いを起動時に弾く
        path = tmp_path / "dedup.yaml"
        path.write_text(
            "thresholds:\n"
            "  title_similarity: 0.9\n"
            "  embedding_similarity: 0.9\n"
            "penalties:\n"
            "  canonical_url: 1.0\n"
            "  normalized_url: 1.0\n"
            "  body_hash: 1.0\n"
            "  title: 0.8\n"
            "  embedding: 0.6\n"
            "unique_value:\n"
            "  content_types: [not_a_content_type]\n"
            "  min_technical_quality: 0.7\n"
            "  max_authority_gap: 0.3\n"
            "  max_candidates_per_cluster: 2\n",
            encoding="utf-8",
        )

        # Act / Assert
        with pytest.raises(ValueError, match="content_types"):
            load_dedup_config(path)

    def test_rejects_an_unknown_key(self, tmp_path: Path):
        # Arrange — 設定漏れに気付けるよう未知のキーを許さない
        path = tmp_path / "dedup.yaml"
        path.write_text("thresholdz: {}\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="thresholdz"):
            load_dedup_config(path)

    def test_reports_a_missing_file(self, tmp_path: Path):
        # Arrange / Act / Assert
        with pytest.raises(DedupConfigError, match="読み込めません"):
            load_dedup_config(tmp_path / "absent.yaml")

    def test_reports_broken_yaml(self, tmp_path: Path):
        # Arrange
        path = tmp_path / "dedup.yaml"
        path.write_text("thresholds: [\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(DedupConfigError, match="YAML"):
            load_dedup_config(path)

    def test_reports_a_non_mapping_document(self, tmp_path: Path):
        # Arrange
        path = tmp_path / "dedup.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(DedupConfigError, match="マッピング"):
            load_dedup_config(path)


class TestConversionToRuleDataclasses:
    """`rules.py` は Pydantic に依存させないため、専用の frozen dataclass へ変換する。"""

    def test_converts_to_duplicate_thresholds(self, config: DedupConfig):
        # Arrange / Act
        thresholds = config.to_thresholds()

        # Assert
        assert isinstance(thresholds, DuplicateThresholds)
        assert thresholds.title_similarity == 0.90

    def test_converts_to_duplicate_penalties(self, config: DedupConfig):
        # Arrange / Act
        penalties = config.to_penalties()

        # Assert
        assert isinstance(penalties, DuplicatePenalties)
        assert penalties.title == 0.8

    def test_converts_to_unique_value_settings(self, config: DedupConfig):
        # Arrange / Act
        settings = config.to_unique_value_settings()

        # Assert
        assert isinstance(settings, UniqueValueSettings)
        assert settings.max_candidates_per_cluster == 2
        assert ContentType.IMPLEMENTATION in settings.content_types
