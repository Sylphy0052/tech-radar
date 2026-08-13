"""novelty 分布の集計（`techradar.measure.novelty`）のテスト（Issue #87）。

文字列一致ベースの `compute_novelty` では、topics を共有しない候補が軒並み
novelty = 1.0 に張り付き、分布を持たない疑いがある。この集計はその疑いを実データで
確かめるための材料であり、ここでは `ScoredCandidate.breakdown.novelty` が既に
持っている値から分布と閾値走査を正しく導けることだけを確かめる（novelty の計算
方法そのものは対象外、`recommendation/` 配下が別途担当する）。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from techradar.measure.novelty import (
    NoveltyDistribution,
    ThresholdSlotCounts,
    summarize_novelty,
    summarize_novelty_distribution,
    summarize_threshold_table,
)
from techradar.recommendation.ranking import (
    AuthorityGate,
    BadSimilaritySettings,
    CandidateSignature,
    FeedComposition,
    FreshnessSettings,
    InterestSettings,
    MatchSettings,
    NoveltySettings,
    RankingLimits,
    ScoreBreakdown,
    ScoredCandidate,
    ScorePenalties,
    ScoreWeights,
    ScoringSettings,
    SourcePreferenceGate,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _settings(*, exploration_min_novelty: float = 0.6) -> ScoringSettings:
    """テスト用の `ScoringSettings` を作る。閾値以外は枠判定に無関係なので固定値にする。"""
    feed_composition = FeedComposition(
        strong_interest=0.55,
        primary_source=0.25,
        exploration=0.15,
        diversity=0.05,
        strong_interest_min_similarity=0.5,
        exploration_min_novelty=exploration_min_novelty,
    )
    return ScoringSettings(
        weights=ScoreWeights(
            interest_similarity=0.35,
            source_authority=0.30,
            source_article_match=0.10,
            freshness=0.10,
            technical_quality=0.10,
            novelty=0.05,
        ),
        penalties=ScorePenalties(bad=1.0, read=0.3),
        authority_gate=AuthorityGate(min_interest_similarity=0.35, min_factor=0.2),
        freshness=FreshnessSettings(max_age_days=7),
        interest=InterestSettings(top_k=3),
        source_match=MatchSettings(partial_match_score=0.5),
        novelty=NoveltySettings(default_when_no_embedding=0.5),
        feed_composition=feed_composition,
        limits=RankingLimits(max_candidates_per_run=500, default_page_size=20, max_page_size=100),
        bad_similarity=BadSimilaritySettings(min_similarity=0.7, max_penalty=0.5),
        source_preference=SourcePreferenceGate(weight_scale=0.15, min_factor=0.5, max_factor=1.5),
    )


def _breakdown(*, interest_similarity: float = 0.0, novelty: float = 0.0) -> ScoreBreakdown:
    """テスト用の `ScoreBreakdown` を作る。枠判定に無関係な項目は 0.0 にする。"""
    return ScoreBreakdown(
        interest_similarity=interest_similarity,
        source_authority=0.0,
        source_article_match=0.0,
        freshness=0.0,
        technical_quality=0.0,
        novelty=novelty,
        authority_gate_factor=1.0,
        source_preference_factor=1.0,
        interest_similarity_contribution=0.0,
        source_authority_contribution=0.0,
        source_article_match_contribution=0.0,
        freshness_contribution=0.0,
        technical_quality_contribution=0.0,
        novelty_contribution=0.0,
        bad_penalty=0.0,
        duplicate_penalty=0.0,
        read_penalty=0.0,
        bad_similarity_penalty=0.0,
        total=0.0,
    )


def _scored(
    *,
    novelty: float,
    interest_similarity: float = 0.0,
    is_primary_source: bool = False,
    source_domain: str = "example.com",
) -> ScoredCandidate:
    """テスト用の `ScoredCandidate` を作る。閾値走査に必要な項目だけ指定できる。"""
    candidate = CandidateSignature(
        id=uuid.uuid4(),
        embedding=None,
        source_authority=0.0,
        is_primary_source=is_primary_source,
        source_domain=source_domain,
        source_entity_names=(),
        topics=(),
        technologies=(),
        technical_quality=0.0,
        published_at=None,
        fetched_at=NOW,
        duplicate_penalty=0.0,
        is_bad=False,
        is_read=False,
    )
    return ScoredCandidate(
        candidate=candidate,
        breakdown=_breakdown(interest_similarity=interest_similarity, novelty=novelty),
    )


class TestSummarizeNoveltyDistribution:
    def test_computes_percentiles_with_linear_interpolation(self) -> None:
        """5 点の等間隔データなら分位点が値そのものと一致するように選ぶ。"""
        values = (0.0, 0.25, 0.5, 0.75, 1.0)

        stats = summarize_novelty_distribution(values, exploration_min_novelty=0.6)

        assert stats.candidate_count == 5
        assert stats.min_novelty == 0.0
        assert stats.p25 == 0.25
        assert stats.p50 == 0.5
        assert stats.p75 == 0.75
        assert stats.p95 == 0.95
        assert stats.max_novelty == 1.0

    def test_counts_values_saturated_at_the_upper_bound(self) -> None:
        """1.0 への張り付きが Issue #87 の核心。件数と割合の両方を持つ。"""
        values = (1.0, 1.0, 1.0, 0.4)

        stats = summarize_novelty_distribution(values, exploration_min_novelty=0.6)

        assert stats.saturated_count == 3
        assert stats.saturated_ratio == 0.75

    def test_counts_values_at_or_above_the_current_threshold(self) -> None:
        """`_slot_for` と同じ `>=` で数える。ちょうど閾値の値も含まれる。"""
        values = (0.0, 0.25, 0.5, 0.6, 0.75, 1.0)

        stats = summarize_novelty_distribution(values, exploration_min_novelty=0.6)

        assert stats.above_threshold_count == 3

    def test_keeps_the_given_threshold_for_reference(self) -> None:
        stats = summarize_novelty_distribution((0.5,), exploration_min_novelty=0.42)

        assert stats.exploration_min_novelty == 0.42

    def test_returns_the_single_value_for_every_percentile_when_only_one_candidate(self) -> None:
        """1 件しかないと補間する余地が無い。全ての分位点がその値になる。"""
        stats = summarize_novelty_distribution((0.42,), exploration_min_novelty=0.5)

        assert stats.min_novelty == 0.42
        assert stats.p25 == 0.42
        assert stats.p50 == 0.42
        assert stats.p75 == 0.42
        assert stats.p95 == 0.42
        assert stats.max_novelty == 0.42

    def test_handles_no_candidates_without_raising(self) -> None:
        """データが揃う前に実行されても落ちない。長さ系は `None` にする。"""
        stats = summarize_novelty_distribution((), exploration_min_novelty=0.6)

        assert stats == NoveltyDistribution(
            candidate_count=0,
            min_novelty=None,
            p25=None,
            p50=None,
            p75=None,
            p95=None,
            max_novelty=None,
            saturated_count=0,
            saturated_ratio=0.0,
            above_threshold_count=0,
            exploration_min_novelty=0.6,
        )


class TestSummarizeThresholdTable:
    def test_returns_eleven_rows_from_zero_to_one_in_tenths(self) -> None:
        rows = summarize_threshold_table((), _settings())

        assert [row.threshold for row in rows] == [
            0.0,
            0.1,
            0.2,
            0.3,
            0.4,
            0.5,
            0.6,
            0.7,
            0.8,
            0.9,
            1.0,
        ]

    def test_moves_a_candidate_from_diversity_to_exploration_as_the_threshold_drops(self) -> None:
        """novelty=0.5 の候補は、閾値が 0.5 以下のときだけ exploration に入る。"""
        candidate = _scored(novelty=0.5)

        rows = {row.threshold: row for row in summarize_threshold_table((candidate,), _settings())}

        assert rows[0.5].exploration_count == 1
        assert rows[0.5].diversity_count == 0
        assert rows[0.6].exploration_count == 0
        assert rows[0.6].diversity_count == 1

    def test_boundary_threshold_equal_to_novelty_counts_as_exploration(self) -> None:
        """`_slot_for` は `>=` で判定する。ちょうど閾値の候補は exploration 側。"""
        candidate = _scored(novelty=0.3)

        rows = {row.threshold: row for row in summarize_threshold_table((candidate,), _settings())}

        assert rows[0.3] == ThresholdSlotCounts(
            threshold=0.3, exploration_count=1, diversity_count=0
        )

    def test_candidates_already_in_strong_interest_never_move_to_exploration_or_diversity(
        self,
    ) -> None:
        """優先順位は strong_interest が novelty より先に効く（`_slot_for` の順序）。

        novelty=1.0（張り付き）でも、関心一致度が閾値を超えていれば strong_interest に
        留まり続け、閾値をどう動かしても exploration / diversity の集計には現れない。
        """
        settings = _settings()
        candidate = _scored(
            novelty=1.0,
            interest_similarity=settings.feed_composition.strong_interest_min_similarity,
        )

        rows = summarize_threshold_table((candidate,), settings)

        assert all(row.exploration_count == 0 and row.diversity_count == 0 for row in rows)

    def test_candidates_already_in_primary_source_never_move_to_exploration_or_diversity(
        self,
    ) -> None:
        candidate = _scored(novelty=1.0, is_primary_source=True)

        rows = summarize_threshold_table((candidate,), _settings())

        assert all(row.exploration_count == 0 and row.diversity_count == 0 for row in rows)

    def test_is_deterministic_and_does_not_mutate_the_given_settings(self) -> None:
        """閾値ごとに設定のコピーを作る。呼び出し元の `settings` を書き換えない。"""
        settings = _settings(exploration_min_novelty=0.6)
        original = replace(settings)
        candidate = _scored(novelty=0.5)

        summarize_threshold_table((candidate,), settings)

        assert settings == original


class TestSummarizeNovelty:
    def test_combines_distribution_and_threshold_table(self) -> None:
        settings = _settings(exploration_min_novelty=0.6)
        candidates = (_scored(novelty=1.0), _scored(novelty=0.2))

        stats = summarize_novelty(candidates, settings)

        assert stats.distribution.candidate_count == 2
        assert stats.distribution.saturated_count == 1
        assert stats.distribution.exploration_min_novelty == 0.6
        assert len(stats.threshold_table) == 11

    def test_handles_no_scored_candidates_without_raising(self) -> None:
        stats = summarize_novelty((), _settings())

        assert stats.distribution.candidate_count == 0
        assert all(
            row.exploration_count == 0 and row.diversity_count == 0 for row in stats.threshold_table
        )
