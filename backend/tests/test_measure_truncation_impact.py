"""切り捨てが解析結果へ与える影響の比較（`techradar.measure.truncation_impact`）のテスト
（Issue #73）。

`MAX_ANALYSIS_BODY_CHARACTERS` を確定するには、応答時間だけでなく「切り捨てた本文」と
「全文」で解析結果がどれだけ変わるかを知る必要がある。ここでは比較・集計の骨組みを
固定する。LLM は呼ばないため、`ArticleAnalysis` の値そのものはテストで決め打ちにする。
"""

from __future__ import annotations

import pytest

from techradar.analysis.schema import ArticleAnalysis
from techradar.db.enums import ContentType, Difficulty
from techradar.measure.truncation_impact import (
    TruncationImpact,
    compare_analyses,
    summarize_truncation_impacts,
)


def _analysis(
    *,
    translated_title: str | None = "題",
    summary_ja: str = "要約",
    domain: str = "AI",
    category: str = "LLM",
    topics: list[str] | None = None,
    technologies: list[str] | None = None,
    content_type: ContentType = ContentType.NEWS,
    difficulty: Difficulty = Difficulty.BEGINNER,
    technical_quality: float = 0.5,
) -> ArticleAnalysis:
    return ArticleAnalysis(
        translated_title=translated_title,
        summary_ja=summary_ja,
        domain=domain,
        category=category,
        topics=topics if topics is not None else ["a", "b"],
        technologies=technologies if technologies is not None else ["x"],
        content_type=content_type,
        difficulty=difficulty,
        technical_quality=technical_quality,
    )


class TestCompareAnalyses:
    def test_reports_exact_match_fields_as_matching_when_identical(self) -> None:
        truncated = _analysis()
        full = _analysis()

        impact = compare_analyses(truncated, full)

        assert impact.domain_matches is True
        assert impact.category_matches is True
        assert impact.content_type_matches is True
        assert impact.difficulty_matches is True

    def test_reports_exact_match_fields_as_differing_when_changed(self) -> None:
        truncated = _analysis(domain="AI", category="LLM", content_type=ContentType.NEWS)
        full = _analysis(domain="Web", category="Frontend", content_type=ContentType.IMPLEMENTATION)

        impact = compare_analyses(truncated, full)

        assert impact.domain_matches is False
        assert impact.category_matches is False
        assert impact.content_type_matches is False

    def test_difficulty_mismatch_is_detected(self) -> None:
        truncated = _analysis(difficulty=Difficulty.BEGINNER)
        full = _analysis(difficulty=Difficulty.ADVANCED)

        impact = compare_analyses(truncated, full)

        assert impact.difficulty_matches is False

    def test_topics_jaccard_is_one_when_identical(self) -> None:
        truncated = _analysis(topics=["MCP", "Agent"])
        full = _analysis(topics=["MCP", "Agent"])

        impact = compare_analyses(truncated, full)

        assert impact.topics_jaccard == pytest.approx(1.0)

    def test_topics_jaccard_ignores_case_and_surrounding_whitespace(self) -> None:
        """表記ゆれ（大文字小文字・前後の空白）で取りこぼさない。"""
        truncated = _analysis(topics=[" MCP ", "Agent"])
        full = _analysis(topics=["mcp", "agent"])

        impact = compare_analyses(truncated, full)

        assert impact.topics_jaccard == pytest.approx(1.0)

    def test_topics_jaccard_is_partial_overlap(self) -> None:
        truncated = _analysis(topics=["a", "b"])
        full = _analysis(topics=["b", "c"])

        impact = compare_analyses(truncated, full)

        # 積集合 {b} / 和集合 {a, b, c} = 1/3
        assert impact.topics_jaccard == pytest.approx(1 / 3)

    def test_topics_jaccard_is_one_when_both_empty(self) -> None:
        """両方空なら要素の点で違いが無いとみなす。"""
        truncated = _analysis(topics=[])
        full = _analysis(topics=[])

        impact = compare_analyses(truncated, full)

        assert impact.topics_jaccard == pytest.approx(1.0)

    def test_topics_jaccard_is_zero_when_one_side_is_empty(self) -> None:
        truncated = _analysis(topics=[])
        full = _analysis(topics=["a"])

        impact = compare_analyses(truncated, full)

        assert impact.topics_jaccard == pytest.approx(0.0)

    def test_technologies_jaccard_is_computed_independently_of_topics(self) -> None:
        truncated = _analysis(topics=["a"], technologies=["x", "y"])
        full = _analysis(topics=["a"], technologies=["y", "z"])

        impact = compare_analyses(truncated, full)

        assert impact.topics_jaccard == pytest.approx(1.0)
        assert impact.technologies_jaccard == pytest.approx(1 / 3)

    def test_technical_quality_diff_is_the_absolute_difference(self) -> None:
        truncated = _analysis(technical_quality=0.3)
        full = _analysis(technical_quality=0.8)

        impact = compare_analyses(truncated, full)

        assert impact.technical_quality_diff == pytest.approx(0.5)

    def test_technical_quality_diff_does_not_depend_on_order(self) -> None:
        """絶対差なので truncated/full の大小が逆でも同じ値になる。"""
        truncated = _analysis(technical_quality=0.8)
        full = _analysis(technical_quality=0.3)

        impact = compare_analyses(truncated, full)

        assert impact.technical_quality_diff == pytest.approx(0.5)

    def test_translated_title_keeps_both_texts_without_judging(self) -> None:
        """translated_title は自動で優劣を判定せず、一致判定と両方のテキストを残す。"""
        truncated = _analysis(translated_title="切り捨て版タイトル")
        full = _analysis(translated_title="全文版タイトル")

        impact = compare_analyses(truncated, full)

        assert impact.translated_title_matches is False
        assert impact.truncated_translated_title == "切り捨て版タイトル"
        assert impact.full_translated_title == "全文版タイトル"

    def test_summary_ja_keeps_both_texts_without_judging(self) -> None:
        truncated = _analysis(summary_ja="短い要約")
        full = _analysis(summary_ja="長い要約です")

        impact = compare_analyses(truncated, full)

        assert impact.summary_matches is False
        assert impact.truncated_summary_ja == "短い要約"
        assert impact.full_summary_ja == "長い要約です"


class TestSummarizeTruncationImpacts:
    def test_returns_none_stats_for_no_impacts(self) -> None:
        """比較できた記事が 0 件でも失敗しない。全記事で LLM 呼び出しが落ちることもある。"""
        summary = summarize_truncation_impacts([], failed_count=3)

        assert summary.compared_count == 0
        assert summary.failed_count == 3
        assert summary.domain_match_rate is None
        assert summary.topics_jaccard_median is None
        assert summary.technical_quality_diff_median is None

    def test_computes_match_rates_across_articles(self) -> None:
        matching = compare_analyses(_analysis(), _analysis())
        differing = compare_analyses(_analysis(domain="A"), _analysis(domain="B"))

        summary = summarize_truncation_impacts([matching, differing])

        assert summary.compared_count == 2
        assert summary.failed_count == 0
        assert summary.domain_match_rate == pytest.approx(0.5)
        # category は両方一致させているので 1.0 のまま。
        assert summary.category_match_rate == pytest.approx(1.0)

    def test_computes_jaccard_and_technical_quality_medians(self) -> None:
        impacts = [
            TruncationImpact(
                domain_matches=True,
                category_matches=True,
                content_type_matches=True,
                difficulty_matches=True,
                topics_jaccard=0.2,
                technologies_jaccard=0.4,
                technical_quality_diff=0.1,
                translated_title_matches=True,
                truncated_translated_title="t",
                full_translated_title="t",
                summary_matches=True,
                truncated_summary_ja="s",
                full_summary_ja="s",
            ),
            TruncationImpact(
                domain_matches=True,
                category_matches=True,
                content_type_matches=True,
                difficulty_matches=True,
                topics_jaccard=0.8,
                technologies_jaccard=0.6,
                technical_quality_diff=0.5,
                translated_title_matches=True,
                truncated_translated_title="t",
                full_translated_title="t",
                summary_matches=True,
                truncated_summary_ja="s",
                full_summary_ja="s",
            ),
        ]

        summary = summarize_truncation_impacts(impacts)

        assert summary.topics_jaccard_median == pytest.approx(0.5)
        assert summary.technologies_jaccard_median == pytest.approx(0.5)
        assert summary.technical_quality_diff_median == pytest.approx(0.3)

    def test_keeps_failed_count_alongside_successful_comparisons(self) -> None:
        """一部の記事が失敗しても、成功分の集計と失敗件数を両方残す。"""
        matching = compare_analyses(_analysis(), _analysis())

        summary = summarize_truncation_impacts([matching], failed_count=1)

        assert summary.compared_count == 1
        assert summary.failed_count == 1
