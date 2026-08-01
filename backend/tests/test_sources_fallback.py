"""レジストリ未登録ドメインの推定を検証する（`PROJECT_SPEC.md` §10）。"""

from __future__ import annotations

import pytest

from techradar.db.enums import SourceType
from techradar.sources.fallback import FallbackConfig, classify_fallback
from techradar.sources.weights import AuthorityWeights

WEIGHTS = AuthorityWeights(
    {
        SourceType.COMPANY_TECH_BLOG: 0.75,
        SourceType.PERSONAL_ARTICLE: 0.6,
        SourceType.TECH_MEDIA: 0.45,
        SourceType.SUMMARY_REPOST: 0.2,
        SourceType.UNKNOWN: 0.35,
    }
)

CONFIG = FallbackConfig(
    domains={
        "zenn.dev": SourceType.PERSONAL_ARTICLE,
        "qiita.com": SourceType.PERSONAL_ARTICLE,
        "publickey1.jp": SourceType.TECH_MEDIA,
        "b.hatena.ne.jp": SourceType.SUMMARY_REPOST,
    },
    host_prefixes=("engineering", "tech", "developer", "devblog"),
    path_hints=("/engineering", "/tech-blog"),
    default_source_type=SourceType.UNKNOWN,
)


class TestKnownDomains:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://zenn.dev/someone/articles/abc", SourceType.PERSONAL_ARTICLE),
            ("https://qiita.com/someone/items/abc", SourceType.PERSONAL_ARTICLE),
            ("https://www.publickey1.jp/blog/26/x.html", SourceType.TECH_MEDIA),
            ("https://b.hatena.ne.jp/entrylist/it", SourceType.SUMMARY_REPOST),
        ],
    )
    def test_classifies_listed_domains(self, url: str, expected: SourceType):
        # Arrange / Act
        result = classify_fallback(url, CONFIG, WEIGHTS)

        # Assert
        assert result.source_type == expected
        assert result.authority_score == WEIGHTS.score_for(expected)
        assert result.is_primary_source is False


class TestHeuristics:
    @pytest.mark.parametrize(
        "url",
        [
            "https://engineering.example.com/post",
            "https://tech.example.co.jp/entry/1",
            "https://developer.example.com/blog/x",
            "https://example.com/engineering/scaling-postgres",
            "https://example.com/tech-blog/2026/x",
        ],
    )
    def test_infers_a_company_tech_blog(self, url: str):
        # Arrange / Act — 企業 Tech Blog は Tier 3。個人記事と混ぜない
        result = classify_fallback(url, CONFIG, WEIGHTS)

        # Assert
        assert result.source_type == SourceType.COMPANY_TECH_BLOG
        assert result.authority_score == 0.75

    def test_does_not_match_a_host_that_merely_starts_with_the_same_text(self):
        # Arrange / Act — `technology.example.com` を `tech` の一致にしない
        result = classify_fallback("https://technology-news.example.com/x", CONFIG, WEIGHTS)

        # Assert
        assert result.source_type == SourceType.UNKNOWN

    def test_falls_back_to_the_default_for_an_unknown_domain(self):
        # Arrange / Act
        result = classify_fallback("https://random-site.example/post/1", CONFIG, WEIGHTS)

        # Assert
        assert result.source_type == SourceType.UNKNOWN
        assert result.authority_score == 0.35

    def test_prefers_the_listed_domain_over_a_heuristic(self):
        # Arrange / Act — 明示登録が推定に負けないこと
        result = classify_fallback("https://tech.qiita.com/items/x", CONFIG, WEIGHTS)

        # Assert
        assert result.source_type == SourceType.PERSONAL_ARTICLE

    def test_returns_the_default_for_an_unparsable_url(self):
        # Arrange / Act
        result = classify_fallback("not a url", CONFIG, WEIGHTS)

        # Assert
        assert result.source_type == SourceType.UNKNOWN


class TestAuthorityWeights:
    def test_uses_the_configured_score(self):
        # Arrange / Act / Assert — 重みは設定ファイル側で管理する
        assert WEIGHTS.score_for(SourceType.TECH_MEDIA) == 0.45

    def test_falls_back_to_the_unknown_score_for_an_unconfigured_type(self):
        # Arrange / Act / Assert — 設定漏れでも判定を止めない
        assert WEIGHTS.score_for(SourceType.ORIGINAL_PAPER) == 0.35
