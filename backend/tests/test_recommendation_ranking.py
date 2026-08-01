"""推薦スコアのランキングロジックを検証する（`PROJECT_SPEC.md` §14, §15）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from techradar.recommendation.ranking import (
    AuthorityGate,
    CandidateSignature,
    FeedComposition,
    FreshnessSettings,
    InterestProfile,
    InterestSettings,
    MatchSettings,
    NoveltySettings,
    RankingLimits,
    ScorePenalties,
    ScoreWeights,
    ScoringSettings,
    build_reason_summary,
    compute_freshness,
    compute_interest_similarity,
    compute_novelty,
    compute_source_article_match,
    cosine_similarity,
    rank_candidates,
    score_candidate,
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
NOVELTY_SETTINGS = NoveltySettings(default_when_no_topics=0.5)
FEED_COMPOSITION = FeedComposition(
    strong_interest=0.55, primary_source=0.25, exploration=0.15, diversity=0.05
)
LIMITS = RankingLimits(max_candidates_per_run=500, default_page_size=20, max_page_size=100)

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
)

EMPTY_PROFILE = InterestProfile(embeddings=(), known_topics=frozenset())


def make_candidate(
    *,
    id: uuid.UUID | None = None,
    embedding: tuple[float, ...] | None = (1.0, 0.0, 0.0),
    source_authority: float = 0.5,
    is_primary_source: bool = False,
    source_entity_names: tuple[str, ...] = (),
    topics: tuple[str, ...] = (),
    technologies: tuple[str, ...] = (),
    technical_quality: float = 0.5,
    published_at: datetime | None = NOW,
    fetched_at: datetime = NOW,
    duplicate_penalty: float = 0.0,
    is_bad: bool = False,
    is_read: bool = False,
) -> CandidateSignature:
    """テスト用の `CandidateSignature` を作る。指定しない項目は無難な既定値にする。"""
    return CandidateSignature(
        id=id or uuid.uuid4(),
        embedding=embedding,
        source_authority=source_authority,
        is_primary_source=is_primary_source,
        source_entity_names=source_entity_names,
        topics=topics,
        technologies=technologies,
        technical_quality=technical_quality,
        published_at=published_at,
        fetched_at=fetched_at,
        duplicate_penalty=duplicate_penalty,
        is_bad=is_bad,
        is_read=is_read,
    )


class TestCosineSimilarity:
    def test_returns_one_for_identical_vectors(self):
        # Arrange / Act / Assert
        assert cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) == pytest.approx(1.0)

    def test_returns_zero_for_orthogonal_vectors(self):
        # Arrange / Act / Assert
        assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)

    def test_returns_zero_when_dimensions_differ(self):
        # Arrange / Act / Assert
        assert cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0)) == 0.0

    def test_returns_zero_for_a_zero_vector(self):
        # Arrange / Act / Assert
        assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0


class TestComputeInterestSimilarity:
    def test_returns_zero_when_the_candidate_has_no_embedding(self):
        # Arrange
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())
        candidate = make_candidate(embedding=None)

        # Act / Assert — 候補に embedding が無い場合は例外を出さず 0.0
        assert compute_interest_similarity(profile, candidate, INTEREST_SETTINGS) == 0.0

    def test_returns_zero_when_the_interest_profile_is_empty(self):
        # Arrange
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act / Assert — 関心プロファイルが空の場合は例外を出さず 0.0
        assert compute_interest_similarity(EMPTY_PROFILE, candidate, INTEREST_SETTINGS) == 0.0

    def test_averages_only_the_top_k_most_similar_embeddings(self):
        # Arrange — top_k=2 なので、最も近い 2 件だけの平均になる
        settings = InterestSettings(top_k=2)
        profile = InterestProfile(
            embeddings=((1.0, 0.0), (0.9, 0.1), (0.0, 1.0)),
            known_topics=frozenset(),
        )
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        similarity = compute_interest_similarity(profile, candidate, settings)

        # Assert — (1.0, 0.0) と (0.9, 0.1) との類似度の平均が、3件全部の平均より高い
        all_settings = InterestSettings(top_k=3)
        similarity_all = compute_interest_similarity(profile, candidate, all_settings)
        assert similarity > similarity_all

    def test_is_deterministic_regardless_of_call_order(self):
        # Arrange
        profile = InterestProfile(
            embeddings=((1.0, 0.0), (0.5, 0.5), (0.0, 1.0)), known_topics=frozenset()
        )
        candidate = make_candidate(embedding=(0.8, 0.2))

        # Act
        first = compute_interest_similarity(profile, candidate, INTEREST_SETTINGS)
        second = compute_interest_similarity(profile, candidate, INTEREST_SETTINGS)

        # Assert
        assert first == second


class TestComputeSourceArticleMatch:
    def test_returns_zero_when_the_candidate_has_no_source_entity_names(self):
        # Arrange
        candidate = make_candidate(source_entity_names=(), topics=("Python",))

        # Act / Assert
        assert compute_source_article_match(candidate, MATCH_SETTINGS) == 0.0

    def test_returns_zero_when_topics_and_technologies_are_both_empty(self):
        # Arrange
        candidate = make_candidate(source_entity_names=("OpenAI",), topics=(), technologies=())

        # Act / Assert
        assert compute_source_article_match(candidate, MATCH_SETTINGS) == 0.0

    def test_returns_one_for_an_exact_normalized_match(self):
        # Arrange — 大文字小文字・前後空白の違いを吸収する
        candidate = make_candidate(source_entity_names=(" OpenAI ",), topics=("openai",))

        # Act / Assert
        assert compute_source_article_match(candidate, MATCH_SETTINGS) == 1.0

    def test_returns_the_partial_match_score_for_a_substring_match(self):
        # Arrange — エンティティ名がトピック文字列に含まれる
        candidate = make_candidate(
            source_entity_names=("OpenAI",), topics=("OpenAI GPT-5の新機能",)
        )

        # Act / Assert
        assert (
            compute_source_article_match(candidate, MATCH_SETTINGS)
            == MATCH_SETTINGS.partial_match_score
        )

    def test_skips_a_blank_entity_name_and_matches_on_the_remaining_ones(self):
        # Arrange — 空白のみのエンティティ名は正規化後に空文字になるため比較対象から外す
        candidate = make_candidate(source_entity_names=("   ", "OpenAI"), topics=("openai",))

        # Act / Assert
        assert compute_source_article_match(candidate, MATCH_SETTINGS) == 1.0

    def test_returns_zero_when_nothing_matches(self):
        # Arrange
        candidate = make_candidate(source_entity_names=("OpenAI",), topics=("量子コンピュータ",))

        # Act / Assert
        assert compute_source_article_match(candidate, MATCH_SETTINGS) == 0.0

    def test_returns_the_maximum_score_across_multiple_candidates(self):
        # Arrange — 1つは部分一致、もう1つは完全一致
        candidate = make_candidate(
            source_entity_names=("Anthropic", "OpenAI"),
            topics=("OpenAIの新機能まとめ", "openai"),
        )

        # Act / Assert
        assert compute_source_article_match(candidate, MATCH_SETTINGS) == 1.0


class TestComputeFreshness:
    def test_returns_one_when_published_today(self):
        # Arrange
        candidate = make_candidate(published_at=NOW)

        # Act / Assert
        assert compute_freshness(candidate, NOW, FRESHNESS_SETTINGS) == 1.0

    def test_returns_zero_beyond_the_max_age(self):
        # Arrange — 7 日超で 0.0
        candidate = make_candidate(published_at=NOW - timedelta(days=8))

        # Act / Assert
        assert compute_freshness(candidate, NOW, FRESHNESS_SETTINGS) == 0.0

    def test_returns_zero_exactly_at_the_max_age(self):
        # Arrange
        candidate = make_candidate(published_at=NOW - timedelta(days=7))

        # Act / Assert
        assert compute_freshness(candidate, NOW, FRESHNESS_SETTINGS) == 0.0

    def test_clamps_a_future_published_at_to_one(self):
        # Arrange — クロックずれなどで未来日付になったケース
        candidate = make_candidate(published_at=NOW + timedelta(days=1))

        # Act / Assert
        assert compute_freshness(candidate, NOW, FRESHNESS_SETTINGS) == 1.0

    def test_uses_fetched_at_when_published_at_is_missing(self):
        # Arrange
        candidate = make_candidate(published_at=None, fetched_at=NOW - timedelta(days=10))

        # Act / Assert
        assert compute_freshness(candidate, NOW, FRESHNESS_SETTINGS) == 0.0

    def test_decays_linearly_between_zero_and_the_max_age(self):
        # Arrange — 3.5 日経過は上限 7 日のちょうど半分
        candidate = make_candidate(published_at=NOW - timedelta(days=3.5))

        # Act / Assert
        assert compute_freshness(candidate, NOW, FRESHNESS_SETTINGS) == pytest.approx(0.5)


class TestComputeNovelty:
    def test_returns_the_default_when_topics_are_empty(self):
        # Arrange
        candidate = make_candidate(topics=())

        # Act / Assert
        assert compute_novelty(candidate, EMPTY_PROFILE, NOVELTY_SETTINGS) == (
            NOVELTY_SETTINGS.default_when_no_topics
        )

    def test_returns_zero_when_all_topics_are_known(self):
        # Arrange
        profile = InterestProfile(embeddings=(), known_topics=frozenset({"python", "rust"}))
        candidate = make_candidate(topics=("Python", "Rust"))

        # Act / Assert — 大文字小文字を無視して既知と判定する
        assert compute_novelty(candidate, profile, NOVELTY_SETTINGS) == 0.0

    def test_returns_one_when_no_topics_are_known(self):
        # Arrange
        profile = InterestProfile(embeddings=(), known_topics=frozenset({"python"}))
        candidate = make_candidate(topics=("量子コンピュータ", "Rust"))

        # Act / Assert
        assert compute_novelty(candidate, profile, NOVELTY_SETTINGS) == 1.0

    def test_returns_the_fraction_of_unknown_topics(self):
        # Arrange — 4 件中 1 件だけ未知
        profile = InterestProfile(embeddings=(), known_topics=frozenset({"python", "rust", "go"}))
        candidate = make_candidate(topics=("Python", "Rust", "Go", "量子コンピュータ"))

        # Act / Assert
        assert compute_novelty(candidate, profile, NOVELTY_SETTINGS) == pytest.approx(0.25)


class TestScoreCandidate:
    def test_is_deterministic_for_the_same_input(self):
        # Arrange — 受入基準: 同じ入力に対して常に同じ出力を返す
        candidate = make_candidate(
            embedding=(0.8, 0.2),
            source_authority=0.7,
            topics=("Python",),
            technical_quality=0.6,
        )
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset({"go"}))

        # Act
        first = score_candidate(candidate, profile, SETTINGS, NOW)
        second = score_candidate(candidate, profile, SETTINGS, NOW)

        # Assert
        assert first == second

    def test_does_not_raise_when_embedding_and_profile_are_both_missing(self):
        # Arrange — 受入基準: embedding が無い候補・空の関心プロファイルで例外を出さず 0.0
        candidate = make_candidate(embedding=None, topics=())

        # Act
        breakdown = score_candidate(candidate, EMPTY_PROFILE, SETTINGS, NOW)

        # Assert
        assert breakdown.interest_similarity == 0.0

    def test_subtracts_the_bad_penalty_when_the_candidate_is_bad(self):
        # Arrange
        good = make_candidate(is_bad=False)
        bad = make_candidate(is_bad=True)
        profile = InterestProfile(embeddings=(), known_topics=frozenset())

        # Act
        good_breakdown = score_candidate(good, profile, SETTINGS, NOW)
        bad_breakdown = score_candidate(bad, profile, SETTINGS, NOW)

        # Assert
        assert bad_breakdown.bad_penalty == PENALTIES.bad
        assert bad_breakdown.total == pytest.approx(good_breakdown.total - PENALTIES.bad)

    def test_subtracts_the_read_penalty_when_the_candidate_is_read(self):
        # Arrange
        unread = make_candidate(is_read=False)
        read = make_candidate(is_read=True)
        profile = InterestProfile(embeddings=(), known_topics=frozenset())

        # Act
        unread_breakdown = score_candidate(unread, profile, SETTINGS, NOW)
        read_breakdown = score_candidate(read, profile, SETTINGS, NOW)

        # Assert
        assert read_breakdown.read_penalty == PENALTIES.read
        assert read_breakdown.total == pytest.approx(unread_breakdown.total - PENALTIES.read)

    def test_subtracts_the_candidates_own_duplicate_penalty_directly(self):
        # Arrange — `articles.duplicate_penalty` をそのまま減点に使う
        original = make_candidate(duplicate_penalty=0.0)
        duplicate = make_candidate(duplicate_penalty=0.6)
        profile = InterestProfile(embeddings=(), known_topics=frozenset())

        # Act
        original_breakdown = score_candidate(original, profile, SETTINGS, NOW)
        duplicate_breakdown = score_candidate(duplicate, profile, SETTINGS, NOW)

        # Assert
        assert duplicate_breakdown.duplicate_penalty == 0.6
        assert duplicate_breakdown.total == pytest.approx(original_breakdown.total - 0.6)

    @pytest.mark.parametrize(("raw", "expected"), [(1.5, 1.0), (-0.5, 0.0), (0.5, 0.5)])
    def test_clamps_technical_quality_to_the_unit_range(self, raw: float, expected: float):
        # Arrange
        candidate = make_candidate(technical_quality=raw)
        profile = InterestProfile(embeddings=(), known_topics=frozenset())

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW)

        # Assert
        assert breakdown.technical_quality == expected

    def test_reasons_contain_the_full_score_breakdown_and_a_japanese_summary(self):
        # Arrange — 受入基準: `reasons` に全項目のスコア内訳が入り、
        # `summary` が日本語 1 文であること
        candidate = make_candidate(
            embedding=(1.0, 0.0),
            source_authority=0.8,
            topics=("Python",),
            technical_quality=0.7,
        )
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW)
        reasons = breakdown.to_reasons()

        # Assert
        for key in (
            "interest_similarity",
            "source_authority",
            "source_article_match",
            "freshness",
            "technical_quality",
            "novelty",
            "authority_gate_factor",
            "interest_similarity_contribution",
            "source_authority_contribution",
            "source_article_match_contribution",
            "freshness_contribution",
            "technical_quality_contribution",
            "novelty_contribution",
            "bad_penalty",
            "duplicate_penalty",
            "read_penalty",
            "total",
        ):
            assert key in reasons
            assert isinstance(reasons[key], float)
        assert isinstance(reasons["summary"], str)
        assert reasons["summary"].endswith("。")


class TestAuthorityGate:
    def test_never_gates_when_the_threshold_itself_is_zero(self):
        # Arrange — 下限が 0 の設定は「常にゲートしない」ことを意味するため、
        # 関心一致度が負（cos 類似度が逆方向）でも係数は 1.0 になる
        # （0 除算のガードも兼ねる）
        gate = AuthorityGate(min_interest_similarity=0.0, min_factor=0.2)
        settings = replace(SETTINGS, authority_gate=gate)
        candidate = make_candidate(embedding=(-1.0, 0.0), source_authority=0.8)
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())

        # Act
        breakdown = score_candidate(candidate, profile, settings, NOW)

        # Assert
        assert breakdown.interest_similarity < 0.0
        assert breakdown.authority_gate_factor == 1.0

    def test_applies_the_full_authority_contribution_when_interest_is_above_the_gate(self):
        # Arrange — interest_similarity が min_interest_similarity 以上ならゲート係数 1.0
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=0.8)
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW)

        # Assert
        assert breakdown.authority_gate_factor == 1.0
        assert breakdown.source_authority_contribution == pytest.approx(
            0.8 * WEIGHTS.source_authority
        )

    def test_applies_the_minimum_factor_when_interest_similarity_is_zero(self):
        # Arrange
        candidate = make_candidate(embedding=(0.0, 1.0), source_authority=0.8)
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW)

        # Assert
        assert breakdown.interest_similarity == pytest.approx(0.0)
        assert breakdown.authority_gate_factor == pytest.approx(AUTHORITY_GATE.min_factor)
        assert breakdown.source_authority_contribution == pytest.approx(
            0.8 * WEIGHTS.source_authority * AUTHORITY_GATE.min_factor
        )

    def test_interpolates_linearly_between_the_minimum_and_the_gate_threshold(self):
        # Arrange — gate.min_interest_similarity（0.8）の半分の類似度（0.4）なら、
        # 係数も min_factor（0.0）と 1.0 のちょうど中間（0.5）になる
        gate = AuthorityGate(min_interest_similarity=0.8, min_factor=0.0)
        settings = replace(SETTINGS, authority_gate=gate)
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=1.0)
        # 類似度がちょうど 0.4 になるよう、cos(theta)=0.4 の向きにする
        profile = InterestProfile(embeddings=((0.4, 0.9165151390),), known_topics=frozenset())

        # Act
        breakdown = score_candidate(candidate, profile, settings, NOW)

        # Assert
        assert breakdown.interest_similarity == pytest.approx(0.4, rel=1e-3)
        assert breakdown.authority_gate_factor == pytest.approx(0.5, rel=1e-3)


class TestRankCandidates:
    def test_is_deterministic_for_the_same_input(self):
        # Arrange — 受入基準: 同じ入力に対して常に同じ出力を返す
        candidates = (
            make_candidate(source_authority=0.9),
            make_candidate(source_authority=0.4),
            make_candidate(source_authority=0.6),
        )
        profile = InterestProfile(embeddings=(), known_topics=frozenset())

        # Act
        first = rank_candidates(candidates, profile, SETTINGS, NOW)
        second = rank_candidates(candidates, profile, SETTINGS, NOW)

        # Assert
        assert tuple(c.candidate.id for c in first) == tuple(c.candidate.id for c in second)

    def test_breaks_ties_by_candidate_id_ascending(self):
        # Arrange — 全項目が同じ候補は total が同点になるため id 昇順が最終判定になる
        first_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        second_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        second = make_candidate(id=second_id)
        first = make_candidate(id=first_id)
        profile = InterestProfile(embeddings=(), known_topics=frozenset())

        # Act
        ranked = rank_candidates((second, first), profile, SETTINGS, NOW)

        # Assert
        assert [c.candidate.id for c in ranked] == [first_id, second_id]

    def test_ranks_the_primary_source_higher_when_interest_similarity_matches_equally(self):
        # Arrange — 受入基準: 関心一致度が同等なら一次情報
        # (is_primary_source=True かつ authority 高) が非公式記事より上位に来る
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())
        primary = make_candidate(embedding=(1.0, 0.0), source_authority=0.9, is_primary_source=True)
        secondary = make_candidate(
            embedding=(1.0, 0.0), source_authority=0.4, is_primary_source=False
        )

        # Act
        ranked = rank_candidates((secondary, primary), profile, SETTINGS, NOW)

        # Assert
        assert [c.candidate.id for c in ranked] == [primary.id, secondary.id]

    def test_does_not_rank_a_low_interest_official_article_above_a_high_interest_unofficial_one(
        self,
    ):
        # Arrange — 受入基準: 関心一致度が極端に低い公式記事は、
        # 関心一致度が高い非公式記事より上位に来ない
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())
        low_interest_official = make_candidate(
            embedding=(0.0, 1.0),
            source_authority=0.95,
            is_primary_source=True,
        )
        high_interest_unofficial = make_candidate(
            embedding=(1.0, 0.0),
            source_authority=0.3,
            is_primary_source=False,
        )

        # Act
        ranked = rank_candidates(
            (low_interest_official, high_interest_unofficial), profile, SETTINGS, NOW
        )

        # Assert
        assert ranked[0].candidate.id == high_interest_unofficial.id

    def test_changing_the_weights_changes_the_ranking(self):
        # Arrange — 受入基準: 重み設定を変えると順位が変わる
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())
        strong_interest_stale = make_candidate(
            embedding=(1.0, 0.0),
            source_authority=0.1,
            published_at=NOW - timedelta(days=6.9),
        )
        weak_interest_fresh = make_candidate(
            embedding=(0.0, 1.0),
            source_authority=0.1,
            published_at=NOW,
        )

        # Act — 既定の重みでは interest_similarity の比重が大きく前者が上位
        default_ranked = rank_candidates(
            (weak_interest_fresh, strong_interest_stale), profile, SETTINGS, NOW
        )

        # freshness を極端に重視し、interest_similarity をゼロにした重みへ差し替える
        freshness_heavy_weights = ScoreWeights(
            interest_similarity=0.0,
            source_authority=0.0,
            source_article_match=0.0,
            freshness=1.0,
            technical_quality=0.0,
            novelty=0.0,
        )
        freshness_heavy_settings = replace(SETTINGS, weights=freshness_heavy_weights)
        freshness_ranked = rank_candidates(
            (weak_interest_fresh, strong_interest_stale), profile, freshness_heavy_settings, NOW
        )

        # Assert
        assert default_ranked[0].candidate.id == strong_interest_stale.id
        assert freshness_ranked[0].candidate.id == weak_interest_fresh.id


class TestBuildReasonSummary:
    def test_returns_a_japanese_sentence_citing_the_top_two_contributions(self):
        # Arrange — interest_similarity と source_authority の寄与が最大になるようにする
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=1.0)
        profile = InterestProfile(embeddings=((1.0, 0.0),), known_topics=frozenset())
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW)

        # Act
        summary = build_reason_summary(breakdown)

        # Assert
        assert summary.endswith("ため、上位に表示しています。")
        assert "関心との一致度が高く" in summary
        assert "情報源の権威性が高い" in summary
