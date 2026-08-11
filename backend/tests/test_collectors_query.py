"""検索クエリ生成（`techradar.collectors.query`）を検証する。"""

from __future__ import annotations

from techradar.collectors.query import build_search_queries

# ja: 「解説」「入門」/ en: "guide", "release" の付加語が使われる前提の期待値。
# `techradar.collectors.query.QUERY_SUFFIXES` が変わったら合わせて更新する。
EXPECTED_TWO_TECHNOLOGIES = (
    "A t 解説",
    "A t 入門",
    "B t 解説",
    "B t 入門",
    "A t guide",
    "A t release",
    "B t guide",
    "B t release",
)


class TestBuildSearchQueries:
    def test_generates_both_japanese_and_english_queries(self):
        # Arrange / Act
        result = build_search_queries(topics=("monitoring",), technologies=("Kubernetes",))

        # Assert — 技術名は原文表記のまま、付加語だけが言語ごとに変わる
        assert "Kubernetes monitoring 解説" in result
        assert "Kubernetes monitoring 入門" in result
        assert "Kubernetes monitoring guide" in result
        assert "Kubernetes monitoring release" in result

    def test_adds_a_third_query_set_when_source_language_is_neither_japanese_nor_english(self):
        # Arrange / Act
        without_source = build_search_queries(topics=("monitoring",), technologies=("Kubernetes",))
        with_french = build_search_queries(
            topics=("monitoring",), technologies=("Kubernetes",), source_language="fr"
        )

        # Assert — 原文言語（フランス語）向けのクエリが追加される
        assert len(with_french) == len(without_source) + 1
        assert "Kubernetes monitoring" in with_french

    def test_does_not_add_extra_queries_when_source_language_is_japanese(self):
        # Arrange / Act
        without_source = build_search_queries(topics=("monitoring",), technologies=("Kubernetes",))
        with_japanese = build_search_queries(
            topics=("monitoring",), technologies=("Kubernetes",), source_language="ja"
        )

        # Assert — 日本語クエリは既に生成済みのため増えない
        assert with_japanese == without_source

    def test_does_not_add_extra_queries_when_source_language_is_english(self):
        # Arrange / Act
        without_source = build_search_queries(topics=("monitoring",), technologies=("Kubernetes",))
        with_english = build_search_queries(
            topics=("monitoring",), technologies=("Kubernetes",), source_language="en-US"
        )

        # Assert — BCP-47 拡張タグ（en-US）でも主要サブタグ en で判定し増えない
        assert with_english == without_source

    def test_returns_empty_tuple_when_topics_and_technologies_are_both_empty(self):
        # Arrange / Act / Assert — 無意味な検索でクォータを消費しない
        assert build_search_queries(topics=(), technologies=()) == ()

    def test_ignores_blank_and_whitespace_only_elements(self):
        # Arrange / Act
        result = build_search_queries(
            topics=(" ", "monitoring"), technologies=("", "  ", "Kubernetes")
        )
        baseline = build_search_queries(topics=("monitoring",), technologies=("Kubernetes",))

        # Assert
        assert result == baseline

    def test_truncates_to_max_queries(self):
        # Arrange / Act
        full = build_search_queries(topics=("t",), technologies=("A", "B"), max_queries=100)
        truncated = build_search_queries(topics=("t",), technologies=("A", "B"), max_queries=3)

        # Assert — 打ち切り後も先頭からの並びは変わらない
        assert truncated == full[:3]
        assert len(truncated) == 3

    def test_is_deterministic_for_the_same_input(self):
        # Arrange / Act
        first = build_search_queries(topics=("t",), technologies=("A", "B"))
        second = build_search_queries(topics=("t",), technologies=("A", "B"))

        # Assert — 同じ入力なら常に同じ順序で返る
        assert first == second == EXPECTED_TWO_TECHNOLOGIES

    def test_returns_an_immutable_tuple(self):
        # Arrange / Act
        result = build_search_queries(topics=("monitoring",), technologies=("Kubernetes",))

        # Assert
        assert isinstance(result, tuple)
