"""Discover フィードの構成比適用を検証する（`PROJECT_SPEC.md` §15）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from techradar.recommendation.composition import (
    ComposedFeed,
    FeedSlot,
    _slot_quotas,
    compose_feed,
    compose_feed_with_stats,
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

WEIGHTS = ScoreWeights(
    interest_similarity=0.35,
    source_authority=0.30,
    source_article_match=0.10,
    freshness=0.10,
    technical_quality=0.10,
    novelty=0.05,
)
PENALTIES = ScorePenalties(bad=1.0, read=0.3)
AUTHORITY_GATE = AuthorityGate(min_interest_similarity=0.35, min_factor=0.2)
FRESHNESS_SETTINGS = FreshnessSettings(max_age_days=7)
INTEREST_SETTINGS = InterestSettings(top_k=3)
MATCH_SETTINGS = MatchSettings(partial_match_score=0.5)
NOVELTY_SETTINGS = NoveltySettings(default_when_no_embedding=0.5)
FEED_COMPOSITION = FeedComposition(
    strong_interest=0.55,
    primary_source=0.25,
    exploration=0.15,
    diversity=0.05,
    strong_interest_min_similarity=0.5,
    exploration_min_novelty=0.6,
)
LIMITS = RankingLimits(max_candidates_per_run=500, default_page_size=20, max_page_size=100)
BAD_SIMILARITY_SETTINGS = BadSimilaritySettings(min_similarity=0.7, max_penalty=0.5)

SETTINGS = ScoringSettings(
    weights=WEIGHTS,
    penalties=PENALTIES,
    authority_gate=AUTHORITY_GATE,
    freshness=FRESHNESS_SETTINGS,
    interest=INTEREST_SETTINGS,
    source_match=MATCH_SETTINGS,
    novelty=NOVELTY_SETTINGS,
    feed_composition=FEED_COMPOSITION,
    limits=LIMITS,
    bad_similarity=BAD_SIMILARITY_SETTINGS,
    source_preference=SourcePreferenceGate(weight_scale=0.15, min_factor=0.5, max_factor=1.5),
)

PAGE_SIZE = 20


def make_breakdown(
    *,
    interest_similarity: float = 0.0,
    novelty: float = 0.0,
    total: float = 0.0,
) -> ScoreBreakdown:
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
        total=total,
    )


def make_scored(
    *,
    id: uuid.UUID | None = None,
    source_domain: str = "example.com",
    is_primary_source: bool = False,
    interest_similarity: float = 0.0,
    novelty: float = 0.0,
    total: float = 0.0,
) -> ScoredCandidate:
    """テスト用の `ScoredCandidate` を作る。枠判定に必要な項目だけ指定できる。"""
    candidate = CandidateSignature(
        id=id or uuid.uuid4(),
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
        breakdown=make_breakdown(
            interest_similarity=interest_similarity, novelty=novelty, total=total
        ),
    )


def sorted_scored(candidates: list[ScoredCandidate]) -> tuple[ScoredCandidate, ...]:
    """`rank_candidates` と同じ並び（スコア降順、同点は id 昇順）に揃える。

    `compose_feed` は入力が既に採点・整列済みであることを前提にしているため、
    テストデータもこの並びで渡す。
    """
    return tuple(sorted(candidates, key=lambda c: (-c.breakdown.total, str(c.candidate.id))))


def make_strong_interest_candidate(
    *, total: float, source_domain: str = "a.example"
) -> ScoredCandidate:
    return make_scored(
        source_domain=source_domain,
        interest_similarity=FEED_COMPOSITION.strong_interest_min_similarity,
        total=total,
    )


def make_primary_source_candidate(
    *, total: float, source_domain: str = "b.example"
) -> ScoredCandidate:
    return make_scored(source_domain=source_domain, is_primary_source=True, total=total)


def make_exploration_candidate(
    *, total: float, source_domain: str = "c.example"
) -> ScoredCandidate:
    return make_scored(
        source_domain=source_domain,
        novelty=FEED_COMPOSITION.exploration_min_novelty,
        total=total,
    )


def make_diversity_candidate(*, total: float, source_domain: str = "d.example") -> ScoredCandidate:
    return make_scored(source_domain=source_domain, total=total)


class TestFeedSlotAssignment:
    def test_assigns_to_strong_interest_when_interest_similarity_meets_the_threshold(self):
        # Arrange
        candidate = make_scored(interest_similarity=0.5, total=1.0)

        # Act
        result = compose_feed_with_stats((candidate,), SETTINGS, PAGE_SIZE)

        # Assert
        strong_interest_stats = next(
            s for s in result.stats.slots if s.slot == FeedSlot.STRONG_INTEREST
        )
        assert strong_interest_stats.selected == 1

    def test_assigns_to_primary_source_when_not_strongly_interesting_but_primary(self):
        # Arrange — 関心一致度は閾値未満、is_primary_source=True
        candidate = make_scored(interest_similarity=0.1, is_primary_source=True, total=1.0)

        # Act
        result = compose_feed_with_stats((candidate,), SETTINGS, PAGE_SIZE)

        # Assert
        primary_stats = next(s for s in result.stats.slots if s.slot == FeedSlot.PRIMARY_SOURCE)
        assert primary_stats.selected == 1

    def test_assigns_to_exploration_when_novelty_meets_the_threshold(self):
        # Arrange
        candidate = make_scored(interest_similarity=0.1, novelty=0.6, total=1.0)

        # Act
        result = compose_feed_with_stats((candidate,), SETTINGS, PAGE_SIZE)

        # Assert
        exploration_stats = next(s for s in result.stats.slots if s.slot == FeedSlot.EXPLORATION)
        assert exploration_stats.selected == 1

    def test_assigns_to_diversity_when_nothing_else_matches(self):
        # Arrange
        candidate = make_scored(interest_similarity=0.1, novelty=0.1, total=1.0)

        # Act
        result = compose_feed_with_stats((candidate,), SETTINGS, PAGE_SIZE)

        # Assert
        diversity_stats = next(s for s in result.stats.slots if s.slot == FeedSlot.DIVERSITY)
        assert diversity_stats.selected == 1

    def test_strong_interest_takes_priority_over_primary_source(self):
        # Arrange — 両方の条件を満たす場合、優先順位トップの strong_interest になる
        candidate = make_scored(interest_similarity=0.9, is_primary_source=True, total=1.0)

        # Act
        result = compose_feed_with_stats((candidate,), SETTINGS, PAGE_SIZE)

        # Assert
        assert result.stats.slots[0].slot == FeedSlot.STRONG_INTEREST
        assert result.stats.slots[0].selected == 1
        primary_stats = next(s for s in result.stats.slots if s.slot == FeedSlot.PRIMARY_SOURCE)
        assert primary_stats.selected == 0


class TestComposeFeedRatios:
    def test_allocates_candidates_by_ratio_when_enough_are_available(self):
        # Arrange — 受入基準: page_size=20 で 11 / 5 / 3 / 1 に配分される
        candidates: list[ScoredCandidate] = []
        for i in range(15):
            candidates.append(
                make_strong_interest_candidate(total=100.0 - i, source_domain=f"strong{i}.example")
            )
        for i in range(10):
            candidates.append(
                make_primary_source_candidate(total=50.0 - i, source_domain=f"primary{i}.example")
            )
        for i in range(10):
            candidates.append(
                make_exploration_candidate(total=20.0 - i, source_domain=f"explore{i}.example")
            )
        for i in range(10):
            candidates.append(
                make_diversity_candidate(total=5.0 - i, source_domain=f"diverse{i}.example")
            )
        scored = sorted_scored(candidates)

        # Act
        result = compose_feed_with_stats(scored, SETTINGS, PAGE_SIZE)

        # Assert
        by_slot = {s.slot: s for s in result.stats.slots}
        assert by_slot[FeedSlot.STRONG_INTEREST].selected == 11
        assert by_slot[FeedSlot.PRIMARY_SOURCE].selected == 5
        assert by_slot[FeedSlot.EXPLORATION].selected == 3
        assert by_slot[FeedSlot.DIVERSITY].selected == 1
        assert sum(s.selected for s in result.stats.slots) == PAGE_SIZE
        assert len(result.candidates) == PAGE_SIZE
        assert all(s.backfilled == 0 for s in result.stats.slots)


class TestComposeFeedBackfill:
    def test_backfills_the_shortfall_from_other_slots_when_strong_interest_is_scarce(self):
        # Arrange — 受入基準: 強い関心一致枠の候補が足りないとき、他の枠から
        # 補充されて合計が page_size になる
        candidates: list[ScoredCandidate] = [
            make_strong_interest_candidate(total=100.0, source_domain="strong0.example"),
            make_strong_interest_candidate(total=99.0, source_domain="strong1.example"),
        ]
        for i in range(30):
            candidates.append(
                make_primary_source_candidate(total=50.0 - i, source_domain=f"primary{i}.example")
            )
        scored = sorted_scored(candidates)

        # Act
        result = compose_feed_with_stats(scored, SETTINGS, PAGE_SIZE)

        # Assert — strong_interest の定員 11 に対し自前の候補は 2 件しか無いため、
        # 9 件分が primary_source 枠の余剰候補（定員 5 を超えた分）から
        # strong_interest 枠の統計として補充される。primary_source 自身は
        # 30 件の候補があり定員 5 を自前で満たせるため補充は発生しない
        by_slot = {s.slot: s for s in result.stats.slots}
        assert by_slot[FeedSlot.STRONG_INTEREST].selected == 11
        assert by_slot[FeedSlot.STRONG_INTEREST].backfilled == 9
        assert by_slot[FeedSlot.PRIMARY_SOURCE].selected == 5
        assert by_slot[FeedSlot.PRIMARY_SOURCE].backfilled == 0
        assert sum(s.selected for s in result.stats.slots) == PAGE_SIZE
        assert len(result.candidates) == PAGE_SIZE

    def test_does_not_raise_and_returns_fewer_when_total_candidates_are_below_page_size(self):
        # Arrange — 受入基準: 候補総数が page_size 未満なら、ある分だけ返る
        candidates = sorted_scored(
            [make_strong_interest_candidate(total=float(i)) for i in range(5)]
        )

        # Act
        result = compose_feed_with_stats(candidates, SETTINGS, PAGE_SIZE)

        # Assert
        assert len(result.candidates) == 5
        assert sum(s.selected for s in result.stats.slots) == 5

    def test_does_not_raise_when_all_slots_are_short_of_quota(self):
        # Arrange — 受入基準: 補充後も定員に満たない枠があっても例外にしない
        candidates = sorted_scored([make_diversity_candidate(total=1.0)])

        # Act
        result = compose_feed_with_stats(candidates, SETTINGS, PAGE_SIZE)

        # Assert
        assert len(result.candidates) == 1


class TestComposeFeedNoDuplicates:
    def test_never_selects_the_same_candidate_twice(self):
        # Arrange — 受入基準: 同じ候補が 2 回選ばれてはならない
        candidates = sorted_scored(
            [make_strong_interest_candidate(total=float(i)) for i in range(3)]
            + [make_primary_source_candidate(total=float(10 + i)) for i in range(3)]
        )

        # Act
        result = compose_feed_with_stats(candidates, SETTINGS, PAGE_SIZE)

        # Assert
        ids = [c.candidate.id for c in result.candidates]
        assert len(ids) == len(set(ids))


class TestComposeFeedDiversitySlot:
    def test_prefers_diversity_candidates_with_a_source_domain_not_already_selected(self):
        # Arrange — strong_interest 枠で a.example を既に選択済みにし、
        # diversity 候補は a.example（高スコア）と b.example（低スコア）を用意する
        strong = make_strong_interest_candidate(total=100.0, source_domain="a.example")
        diversity_same_domain = make_diversity_candidate(total=10.0, source_domain="a.example")
        diversity_new_domain = make_diversity_candidate(total=5.0, source_domain="b.example")
        scored = sorted_scored([strong, diversity_same_domain, diversity_new_domain])

        # Act
        result = compose_feed_with_stats(scored, SETTINGS, PAGE_SIZE)

        # Assert — ドメイン重複しない b.example が多様性枠として優先的に選ばれる
        diversity_ids = [
            c.candidate.id for c in result.candidates if c.candidate.source_domain == "b.example"
        ]
        assert diversity_same_domain.candidate.id in [c.candidate.id for c in result.candidates]
        assert diversity_new_domain.candidate.id in [c.candidate.id for c in result.candidates]
        assert diversity_ids == [diversity_new_domain.candidate.id]

    def test_diversity_slot_is_left_short_when_only_duplicate_domains_remain(self):
        # Arrange — 受入基準: 重複しないものが尽きたら、その枠は残りを埋めない
        # （縮退は backfill が吸収する）
        strong = make_strong_interest_candidate(total=100.0, source_domain="a.example")
        diversity_dup_1 = make_diversity_candidate(total=10.0, source_domain="a.example")
        diversity_dup_2 = make_diversity_candidate(total=9.0, source_domain="a.example")
        scored = sorted_scored([strong, diversity_dup_1, diversity_dup_2])

        # Act
        result = compose_feed_with_stats(scored, SETTINGS, PAGE_SIZE)

        # Assert — diversity 枠自体は重複ドメインの候補を選ばない（own selection は
        # 0 のまま）。取りこぼした 2 件は他の枠（この場合は定員が大きい
        # strong_interest）の補充で拾われ、最終的には両方とも出力に含まれる
        by_slot = {s.slot: s for s in result.stats.slots}
        assert by_slot[FeedSlot.DIVERSITY].selected == 0
        assert sum(s.backfilled for s in result.stats.slots) == 2
        result_ids = {c.candidate.id for c in result.candidates}
        assert diversity_dup_1.candidate.id in result_ids
        assert diversity_dup_2.candidate.id in result_ids
        assert len(result.candidates) == 3


class TestComposeFeedDeterminism:
    def test_returns_the_same_result_for_the_same_input(self):
        # Arrange — 受入基準: 決定的（同じ入力で同じ出力）
        candidates = sorted_scored(
            [make_strong_interest_candidate(total=float(i)) for i in range(5)]
            + [make_primary_source_candidate(total=float(10 + i)) for i in range(5)]
            + [make_exploration_candidate(total=float(20 + i)) for i in range(5)]
            + [make_diversity_candidate(total=float(30 + i)) for i in range(5)]
        )

        # Act
        first = compose_feed(candidates, SETTINGS, PAGE_SIZE)
        second = compose_feed(candidates, SETTINGS, PAGE_SIZE)

        # Assert
        assert tuple(c.candidate.id for c in first) == tuple(c.candidate.id for c in second)


class TestComposeFeedEmptyInput:
    def test_returns_empty_for_empty_input_without_raising(self):
        # Arrange / Act
        result = compose_feed((), SETTINGS, PAGE_SIZE)

        # Assert
        assert result == ()

    def test_compose_feed_with_stats_returns_empty_candidates_for_empty_input(self):
        # Arrange / Act
        result = compose_feed_with_stats((), SETTINGS, PAGE_SIZE)

        # Assert
        assert isinstance(result, ComposedFeed)
        assert result.candidates == ()
        assert sum(s.selected for s in result.stats.slots) == 0


class TestComposeFeedOutputOrder:
    def test_orders_the_final_output_by_score_descending_regardless_of_slot(self):
        # Arrange — 受入基準: 出力順は最終的にスコア降順で並べ直す
        candidates = sorted_scored(
            [
                make_strong_interest_candidate(total=10.0),
                make_primary_source_candidate(total=50.0),
                make_exploration_candidate(total=30.0),
                make_diversity_candidate(total=90.0),
            ]
        )

        # Act
        result = compose_feed(candidates, SETTINGS, PAGE_SIZE)

        # Assert
        totals = [c.breakdown.total for c in result]
        assert totals == sorted(totals, reverse=True)

    def test_breaks_ties_by_candidate_id_ascending(self):
        # Arrange
        first_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        second_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        first = make_scored(id=first_id, total=1.0)
        second = make_scored(id=second_id, total=1.0)
        scored = sorted_scored([second, first])

        # Act
        result = compose_feed(scored, SETTINGS, PAGE_SIZE)

        # Assert
        assert [c.candidate.id for c in result] == [first_id, second_id]


class TestSlotStatsConsistency:
    def test_stats_match_the_actual_selected_candidates(self):
        # Arrange — 受入基準: 統計（定員・実選択数・補充数）が実際の結果と一致する
        candidates = sorted_scored(
            [make_strong_interest_candidate(total=100.0, source_domain="strong0.example")]
            + [
                make_primary_source_candidate(total=50.0 - i, source_domain=f"primary{i}.example")
                for i in range(30)
            ]
        )

        # Act
        result = compose_feed_with_stats(candidates, SETTINGS, PAGE_SIZE)

        # Assert
        assert sum(s.selected for s in result.stats.slots) == len(result.candidates)
        for slot_stats in result.stats.slots:
            assert slot_stats.backfilled <= slot_stats.selected
            assert slot_stats.selected <= slot_stats.quota


class TestSlotQuotasRounding:
    """`_slot_quotas` の丸め調整ロジックを直接検証する。

    既定比率（0.55/0.25/0.15/0.05）は page_size=20 だとぴったり合うため、
    ここでは意図的に丸め誤差が出る page_size・比率を選んで検証する。
    """

    def test_sums_to_page_size_when_rounding_undershoots(self):
        # Arrange — 既定比率で page_size=2 だと round() の合計が 1（strong のみ）に
        # とどまり page_size に届かないため、比率が最大の枠へ 1 件加算される
        # Act
        quotas = _slot_quotas(2, SETTINGS)

        # Assert
        assert quotas == {
            FeedSlot.STRONG_INTEREST: 2,
            FeedSlot.PRIMARY_SOURCE: 0,
            FeedSlot.EXPLORATION: 0,
            FeedSlot.DIVERSITY: 0,
        }
        assert sum(quotas.values()) == 2

    def test_sums_to_page_size_when_page_size_is_smaller_than_the_number_of_slots(self):
        # Arrange — 受入基準: page_size が小さく丸めで 0 になる枠がある場合でも
        # 合計は必ず page_size に一致する
        # Act
        quotas = _slot_quotas(1, SETTINGS)

        # Assert
        assert sum(quotas.values()) == 1
        assert quotas[FeedSlot.STRONG_INTEREST] == 1
        assert quotas[FeedSlot.PRIMARY_SOURCE] == 0
        assert quotas[FeedSlot.EXPLORATION] == 0
        assert quotas[FeedSlot.DIVERSITY] == 0

    def test_sums_to_page_size_when_rounding_overshoots(self):
        # Arrange — 比率を 0.15/0.15/0.35/0.35 にすると page_size=10 で
        # round() の合計が 12（2+2+4+4）になり超過するため、比率が大きい枠から
        # 順に 1 件ずつ減らして 10 に一致させる調整が必要になる
        overshoot_composition = FeedComposition(
            strong_interest=0.15,
            primary_source=0.15,
            exploration=0.35,
            diversity=0.35,
            strong_interest_min_similarity=0.5,
            exploration_min_novelty=0.6,
        )
        overshoot_settings = replace(SETTINGS, feed_composition=overshoot_composition)

        # Act
        quotas = _slot_quotas(10, overshoot_settings)

        # Assert
        assert quotas == {
            FeedSlot.STRONG_INTEREST: 2,
            FeedSlot.PRIMARY_SOURCE: 2,
            FeedSlot.EXPLORATION: 3,
            FeedSlot.DIVERSITY: 3,
        }
        assert sum(quotas.values()) == 10

    def test_is_deterministic_for_the_same_input(self):
        # Arrange / Act — 受入基準: 同じ入力なら同じ結果（丸め調整も決定的）
        first = _slot_quotas(7, SETTINGS)
        second = _slot_quotas(7, SETTINGS)

        # Assert
        assert first == second
