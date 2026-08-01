"""設定ファイルの読み込みと、実レジストリでの判定を検証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from techradar.db.enums import SourceType
from techradar.sources.classifier import classify
from techradar.sources.config import (
    DEFAULT_CONFIG_PATH,
    SourceConfigError,
    load_registry_config,
)


@pytest.fixture(scope="module")
def config():
    """同梱の `config/sources.yaml`。"""
    return load_registry_config()


class TestLoading:
    def test_loads_the_bundled_registry(self, config):
        # Arrange / Act / Assert — 同梱ファイルが常に読める（起動時に落ちない）
        assert DEFAULT_CONFIG_PATH.exists()
        assert len(config.entities) >= 14

    def test_covers_the_entities_required_by_the_spec(self, config):
        # Arrange — `PROJECT_SPEC.md` §11 の初期対象
        required = {
            "OpenAI",
            "Anthropic",
            "Google",
            "Microsoft",
            "AWS",
            "Meta",
            "Hugging Face",
            "GitHub",
            "GitLab",
            "Cloudflare",
            "NVIDIA",
            "Python",
            "Rust",
            "TypeScript",
        }

        # Act
        names = {entity.name for entity in config.entities}

        # Assert
        assert required <= names

    def test_applies_the_default_authority_of_each_source_type(self, config):
        # Arrange / Act — `authority` 未指定の規則に重みが当たること
        rules = {(rule.domain, rule.path_pattern): rule for rule in config.to_rules()}

        # Assert
        assert rules[("platform.openai.com", "/docs")].authority_score == 1.0
        assert rules[("openai.com", "/index")].authority_score == 0.9

    def test_rejects_an_unknown_source_type(self, tmp_path: Path):
        # Arrange — 種別の打ち間違いを起動時に弾く
        path = tmp_path / "sources.yaml"
        path.write_text(
            "entities:\n  - name: X\n    rules:\n      - domain: x.example\n"
            "        type: not_a_source_type\n",
            encoding="utf-8",
        )

        # Act / Assert
        with pytest.raises(ValueError, match="type"):
            load_registry_config(path)

    def test_rejects_an_unknown_key(self, tmp_path: Path):
        # Arrange — 設定漏れに気付けるよう未知のキーを許さない
        path = tmp_path / "sources.yaml"
        path.write_text("entitites: []\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="entitites"):
            load_registry_config(path)

    def test_reports_a_missing_file(self, tmp_path: Path):
        # Arrange / Act / Assert
        with pytest.raises(SourceConfigError, match="読み込めません"):
            load_registry_config(tmp_path / "absent.yaml")

    def test_reports_broken_yaml(self, tmp_path: Path):
        # Arrange
        path = tmp_path / "sources.yaml"
        path.write_text("entities: [\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(SourceConfigError, match="YAML"):
            load_registry_config(path)

    def test_reports_a_non_mapping_document(self, tmp_path: Path):
        # Arrange
        path = tmp_path / "sources.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(SourceConfigError, match="マッピング"):
            load_registry_config(path)


class TestClassificationWithTheBundledRegistry:
    @pytest.mark.parametrize(
        ("url", "source_type", "authority"),
        [
            # 受入基準
            (
                "https://platform.openai.com/docs/api-reference/chat",
                SourceType.OFFICIAL_DOCUMENTATION,
                1.0,
            ),
            ("https://openai.com/index/introducing-gpt-5/", SourceType.OFFICIAL_BLOG, 0.9),
            (
                "https://github.com/anthropics/anthropic-sdk-python/releases/tag/v1.0.0",
                SourceType.OFFICIAL_GITHUB_RELEASE,
                0.9,
            ),
            # その他の主要ソース
            ("https://docs.claude.com/en/docs/mcp", SourceType.OFFICIAL_DOCUMENTATION, 1.0),
            ("https://arxiv.org/abs/2501.00001", SourceType.ORIGINAL_PAPER, 0.95),
            ("https://peps.python.org/pep-0750/", SourceType.STANDARD_SPECIFICATION, 1.0),
            ("https://github.blog/changelog/2026-01-01-x/", SourceType.OFFICIAL_RELEASE_NOTES, 1.0),
            (
                "https://blog.rust-lang.org/2026/01/01/Rust-1.90.0.html",
                SourceType.OFFICIAL_BLOG,
                0.9,
            ),
        ],
    )
    def test_classifies_official_sources(
        self, config, url: str, source_type: SourceType, authority: float
    ):
        # Arrange
        rules = config.to_rules()

        # Act
        result = classify(url, rules, config.to_fallback_config(), config.to_weights())

        # Assert
        assert result.source_type == source_type
        assert result.authority_score == authority
        assert result.is_primary_source is True

    @pytest.mark.parametrize(
        ("url", "source_type", "authority"),
        [
            ("https://zenn.dev/someone/articles/abc", SourceType.PERSONAL_ARTICLE, 0.6),
            ("https://www.publickey1.jp/blog/26/x.html", SourceType.TECH_MEDIA, 0.45),
            ("https://b.hatena.ne.jp/entrylist/it", SourceType.SUMMARY_REPOST, 0.2),
            ("https://engineering.example.com/post", SourceType.COMPANY_TECH_BLOG, 0.75),
            ("https://unknown-site.example/post", SourceType.UNKNOWN, 0.35),
        ],
    )
    def test_falls_back_for_unregistered_domains(
        self, config, url: str, source_type: SourceType, authority: float
    ):
        # Arrange / Act — 受入基準「未登録ドメインが妥当な Tier に落ちる」
        result = classify(url, config.to_rules(), config.to_fallback_config(), config.to_weights())

        # Assert
        assert result.source_type == source_type
        assert result.authority_score == authority
        assert result.is_primary_source is False

    def test_does_not_treat_a_third_party_github_release_as_official(self, config):
        # Arrange / Act — 公式 org 以外の Release を公式扱いしない
        result = classify(
            "https://github.com/some-user/anthropic-sdk-python/releases/tag/v1.0.0",
            config.to_rules(),
            config.to_fallback_config(),
            config.to_weights(),
        )

        # Assert
        assert result.source_type != SourceType.OFFICIAL_GITHUB_RELEASE
        assert result.is_primary_source is False
