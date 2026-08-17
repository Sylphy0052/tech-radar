"""novelty 分布の集計（`techradar.measure.novelty`）のテスト（Issue #87、Issue #88）。

文字列一致ベースの `compute_novelty` では、topics を共有しない候補が軒並み
novelty = 1.0 に張り付き、分布を持たない疑いがある。この集計はその疑いを実データで
確かめるための材料であり、ここでは `ScoredCandidate.breakdown.novelty` が既に
持っている値から分布・閾値走査・縮退の兆候（相関・枠の偏り）を正しく導けることだけを
確かめる（novelty の計算方法そのものは対象外、`recommendation/` 配下が別途担当する）。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from techradar.measure.novelty import (
    NoveltyDistribution,
    SlotDivergenceStats,
    ThresholdSlotCounts,
    summarize_novelty,
    summarize_novelty_distribution,
    summarize_novelty_interest_correlation,
    summarize_slot_divergence,
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

    def test_wires_the_correlation_and_slot_divergence_from_the_same_candidates(self) -> None:
        """相関と枠の偏りも、単体関数を直接呼んだ結果と一致する（配線の確認）。"""
        settings = _settings(exploration_min_novelty=0.6)
        candidates = (
            _scored(novelty=0.9, interest_similarity=0.1),
            _scored(novelty=0.2, interest_similarity=0.4),
        )

        stats = summarize_novelty(candidates, settings)

        assert stats.novelty_interest_correlation == summarize_novelty_interest_correlation(
            candidates
        )
        assert stats.slot_divergence == summarize_slot_divergence(candidates, settings)

    def test_handles_no_scored_candidates_without_raising(self) -> None:
        stats = summarize_novelty((), _settings())

        assert stats.distribution.candidate_count == 0
        assert all(
            row.exploration_count == 0 and row.diversity_count == 0 for row in stats.threshold_table
        )
        assert stats.novelty_interest_correlation is None
        assert stats.slot_divergence == SlotDivergenceStats(
            excluded_count=0, diversity_count=0, diversity_ratio=None
        )


class TestSummarizeNoveltyInterestCorrelation:
    def test_returns_none_when_all_novelty_values_are_the_same(self) -> None:
        """novelty の分散がゼロだと順位が全て同順位になり、相関を定義できない。"""
        candidates = (
            _scored(novelty=0.5, interest_similarity=0.1),
            _scored(novelty=0.5, interest_similarity=0.2),
            _scored(novelty=0.5, interest_similarity=0.3),
        )

        correlation = summarize_novelty_interest_correlation(candidates)

        assert correlation is None

    def test_returns_minus_one_when_novelty_is_the_exact_complement(self) -> None:
        """novelty が `1 - interest_similarity` と完全一致 = 縮退の極。相関は -1.0。"""
        candidates = (
            _scored(novelty=0.9, interest_similarity=0.1),
            _scored(novelty=0.8, interest_similarity=0.2),
            _scored(novelty=0.7, interest_similarity=0.3),
        )

        correlation = summarize_novelty_interest_correlation(candidates)

        assert correlation == -1.0

    def test_returns_zero_when_ranks_are_unrelated(self) -> None:
        """順位が完全に無関係な入力（手計算で 0 になるよう組んだ順列）では 0 付近になる。"""
        candidates = (
            _scored(novelty=0.1, interest_similarity=0.2),
            _scored(novelty=0.2, interest_similarity=0.4),
            _scored(novelty=0.3, interest_similarity=0.1),
            _scored(novelty=0.4, interest_similarity=0.3),
        )

        correlation = summarize_novelty_interest_correlation(candidates)

        assert correlation == 0.0

    def test_uses_average_rank_for_ties(self) -> None:
        """同順位 (tie) を平均順位で扱う。手計算で検算できる 3 件で固定する。

        novelty = (0.5, 0.5, 0.2) の順位は (2.5, 2.5, 1)（先頭 2 件が 2 位・3 位を
        分け合う）。interest_similarity = (0.1, 0.2, 0.3) の順位は (1, 2, 3)。
        共分散 -1.5、両者の分散はそれぞれ 1.5 と 2.0 になり、相関は
        -1.5 / sqrt(1.5 * 2.0) = -sqrt(3) / 2 になる。
        """
        candidates = (
            _scored(novelty=0.5, interest_similarity=0.1),
            _scored(novelty=0.5, interest_similarity=0.2),
            _scored(novelty=0.2, interest_similarity=0.3),
        )

        correlation = summarize_novelty_interest_correlation(candidates)

        assert correlation is not None
        assert math.isclose(correlation, -math.sqrt(3) / 2)

    def test_handles_no_candidates_without_raising(self) -> None:
        assert summarize_novelty_interest_correlation(()) is None

    def test_handles_a_single_candidate_without_raising(self) -> None:
        """1 件だけでは順位に差が出ようがなく、相関を定義できない。"""
        candidates = (_scored(novelty=0.5, interest_similarity=0.3),)

        assert summarize_novelty_interest_correlation(candidates) is None


class TestSummarizeSlotDivergence:
    def test_returns_zero_percent_when_all_excluded_candidates_land_in_exploration(self) -> None:
        """全件が exploration へ流れる = 片方への張り付き（縮退の兆候）。"""
        candidates = (
            _scored(novelty=0.7, interest_similarity=0.1),
            _scored(novelty=0.8, interest_similarity=0.2),
        )

        stats = summarize_slot_divergence(candidates, _settings())

        assert stats == SlotDivergenceStats(
            excluded_count=2, diversity_count=0, diversity_ratio=0.0
        )

    def test_returns_hundred_percent_when_all_excluded_candidates_land_in_diversity(self) -> None:
        """全件が diversity へ流れる = 逆側への張り付き（Issue #87 で実際に起きた症状）。"""
        candidates = (
            _scored(novelty=0.3, interest_similarity=0.1),
            _scored(novelty=0.4, interest_similarity=0.2),
        )

        stats = summarize_slot_divergence(candidates, _settings())

        assert stats == SlotDivergenceStats(
            excluded_count=2, diversity_count=2, diversity_ratio=1.0
        )

    def test_returns_none_ratio_when_no_candidate_is_excluded_from_the_top_two_slots(self) -> None:
        """全候補が strong_interest に収まると比較対象が無い。0 と混同しない。"""
        settings = _settings()
        candidates = (
            _scored(
                novelty=1.0,
                interest_similarity=settings.feed_composition.strong_interest_min_similarity,
            ),
        )

        stats = summarize_slot_divergence(candidates, settings)

        assert stats == SlotDivergenceStats(
            excluded_count=0, diversity_count=0, diversity_ratio=None
        )

    def test_handles_no_candidates_without_raising(self) -> None:
        stats = summarize_slot_divergence((), _settings())

        assert stats == SlotDivergenceStats(
            excluded_count=0, diversity_count=0, diversity_ratio=None
        )

    def test_handles_a_single_candidate_without_raising(self) -> None:
        candidate = _scored(novelty=0.3, interest_similarity=0.1)

        stats = summarize_slot_divergence((candidate,), _settings())

        assert stats == SlotDivergenceStats(
            excluded_count=1, diversity_count=1, diversity_ratio=1.0
        )
