"""情報源の分類ロジックを検証する（`PROJECT_SPEC.md` §10, §11）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import pytest

from techradar.db.enums import SourceType
from techradar.sources.rules import (
    SourceRule,
    classify_url,
    is_primary_source,
    tier_of,
)

OPENAI_RULES = (
    SourceRule(
        entity_name="OpenAI",
        domain="platform.openai.com",
        path_pattern="/docs",
        source_type=SourceType.OFFICIAL_DOCUMENTATION,
        authority_score=1.0,
    ),
    SourceRule(
        entity_name="OpenAI",
        domain="openai.com",
        path_pattern="/index",
        source_type=SourceType.OFFICIAL_BLOG,
        authority_score=0.9,
    ),
    SourceRule(
        entity_name="OpenAI",
        domain="github.com",
        path_pattern="/*/*/releases",
        github_org="openai",
        source_type=SourceType.OFFICIAL_GITHUB_RELEASE,
        authority_score=0.9,
    ),
)


class TestAcceptanceCriteria:
    def test_classifies_openai_documentation(self):
        # Arrange / Act
        result = classify_url("https://platform.openai.com/docs/api-reference", OPENAI_RULES)

        # Assert
        assert result.source_type == SourceType.OFFICIAL_DOCUMENTATION
        assert result.authority_score == 1.0
        assert result.entity_name == "OpenAI"
        assert result.is_primary_source is True

    def test_classifies_openai_blog(self):
        # Arrange / Act
        result = classify_url("https://openai.com/index/introducing-gpt-5/", OPENAI_RULES)

        # Assert
        assert result.source_type == SourceType.OFFICIAL_BLOG
        assert result.authority_score == 0.9
        assert result.is_primary_source is True

    def test_classifies_github_release_of_an_official_org(self):
        # Arrange / Act
        result = classify_url(
            "https://github.com/openai/openai-python/releases/tag/v1.0", OPENAI_RULES
        )

        # Assert
        assert result.source_type == SourceType.OFFICIAL_GITHUB_RELEASE
        assert result.authority_score == 0.9
        assert result.entity_name == "OpenAI"

    def test_does_not_treat_a_third_party_fork_as_official(self):
        # Arrange / Act — org が一致しなければ公式扱いしない。
        # ここを緩めると誰でも公式 authority を名乗れる
        result = classify_url(
            "https://github.com/someone-else/openai-python/releases/tag/v1.0", OPENAI_RULES
        )

        # Assert
        assert result.source_type != SourceType.OFFICIAL_GITHUB_RELEASE
        assert result.entity_name is None


class TestMatching:
    def test_matches_subdomains_of_a_registered_domain(self):
        # Arrange
        rules = (
            SourceRule(
                entity_name="Example",
                domain="example.com",
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.9,
            ),
        )

        # Act
        result = classify_url("https://blog.example.com/post", rules)

        # Assert
        assert result.entity_name == "Example"

    def test_does_not_match_a_domain_that_merely_ends_with_the_same_text(self):
        # Arrange — `notexample.com` を `example.com` の一致にしない
        rules = (
            SourceRule(
                entity_name="Example",
                domain="example.com",
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.9,
            ),
        )

        # Act
        result = classify_url("https://notexample.com/post", rules)

        # Assert
        assert result.entity_name is None

    def test_prefers_the_most_specific_path_pattern(self):
        # Arrange — ドメイン全体の規則より、深いパスの規則を優先する
        rules = (
            SourceRule(
                entity_name="Example",
                domain="example.com",
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.9,
            ),
            SourceRule(
                entity_name="Example",
                domain="example.com",
                path_pattern="/docs/api",
                source_type=SourceType.API_SPECIFICATION,
                authority_score=1.0,
            ),
            SourceRule(
                entity_name="Example",
                domain="example.com",
                path_pattern="/docs",
                source_type=SourceType.OFFICIAL_DOCUMENTATION,
                authority_score=1.0,
            ),
        )

        # Act
        result = classify_url("https://example.com/docs/api/v1", rules)

        # Assert
        assert result.source_type == SourceType.API_SPECIFICATION

    def test_prefers_an_exact_host_over_a_parent_domain(self):
        # Arrange
        rules = (
            SourceRule(
                entity_name="Example",
                domain="example.com",
                source_type=SourceType.COMPANY_TECH_BLOG,
                authority_score=0.75,
            ),
            SourceRule(
                entity_name="Example Docs",
                domain="docs.example.com",
                source_type=SourceType.OFFICIAL_DOCUMENTATION,
                authority_score=1.0,
            ),
        )

        # Act
        result = classify_url("https://docs.example.com/guide", rules)

        # Assert
        assert result.entity_name == "Example Docs"

    def test_matches_a_single_segment_with_a_wildcard(self):
        # Arrange — GitHub の `/<org>/<repo>/releases` のように可変部分を挟む形
        rules = (
            SourceRule(
                entity_name="Example",
                domain="example.com",
                path_pattern="/*/docs",
                source_type=SourceType.OFFICIAL_DOCUMENTATION,
                authority_score=1.0,
            ),
        )

        # Act / Assert
        assert classify_url("https://example.com/v2/docs/intro", rules).entity_name == "Example"
        # ワイルドカードは 1 セグメントのみ。階層をまたいで一致させない
        assert classify_url("https://example.com/a/b/docs", rules).entity_name is None

    def test_matches_a_path_only_on_segment_boundaries(self):
        # Arrange — `/docs` の規則が `/documentation` に当たらないこと
        rules = (
            SourceRule(
                entity_name="Example",
                domain="example.com",
                path_pattern="/docs",
                source_type=SourceType.OFFICIAL_DOCUMENTATION,
                authority_score=1.0,
            ),
        )

        # Act
        result = classify_url("https://example.com/documentation/intro", rules)

        # Assert
        assert result.entity_name is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://PLATFORM.OpenAI.com/docs/api-reference",
            "https://platform.openai.com:443/docs/api-reference",
            "https://platform.openai.com/docs/api-reference?utm_source=x#section",
        ],
    )
    def test_ignores_representation_differences(self, url: str):
        # Arrange / Act — 大文字・既定ポート・計測パラメータで判定が変わらないこと
        result = classify_url(url, OPENAI_RULES)

        # Assert
        assert result.source_type == SourceType.OFFICIAL_DOCUMENTATION

    def test_returns_unknown_for_an_unparsable_url(self):
        # Arrange / Act — ホストを取り出せない入力でも例外にしない
        result = classify_url("not a url", ())

        # Assert
        assert result.source_type == SourceType.UNKNOWN
        assert result.entity_name is None


class TestTier:
    @pytest.mark.parametrize(
        ("source_type", "expected"),
        [
            (SourceType.OFFICIAL_DOCUMENTATION, 1),
            (SourceType.API_SPECIFICATION, 1),
            (SourceType.STANDARD_SPECIFICATION, 1),
            (SourceType.OFFICIAL_RELEASE_NOTES, 1),
            (SourceType.OFFICIAL_BLOG, 2),
            (SourceType.OFFICIAL_RESEARCH, 2),
            (SourceType.ORIGINAL_PAPER, 2),
            (SourceType.OFFICIAL_GITHUB_RELEASE, 2),
            (SourceType.COMPANY_TECH_BLOG, 3),
            (SourceType.MAINTAINER_ARTICLE, 3),
            (SourceType.PERSONAL_ARTICLE, 4),
            (SourceType.TECH_MEDIA, 4),
            (SourceType.NEWS_REPOST, 5),
            (SourceType.SUMMARY_REPOST, 5),
            (SourceType.UNKNOWN, 5),
        ],
    )
    def test_maps_every_source_type_to_a_tier(self, source_type: SourceType, expected: int):
        # Arrange / Act / Assert — 未定義の種別を残すと判定が破綻する
        assert tier_of(source_type) == expected

    def test_treats_tier_1_and_2_as_primary_sources(self):
        # Arrange / Act / Assert — 一次情報の定義（`PROJECT_SPEC.md` §10）
        assert is_primary_source(SourceType.OFFICIAL_DOCUMENTATION) is True
        assert is_primary_source(SourceType.OFFICIAL_GITHUB_RELEASE) is True
        assert is_primary_source(SourceType.COMPANY_TECH_BLOG) is False
        assert is_primary_source(SourceType.UNKNOWN) is False
