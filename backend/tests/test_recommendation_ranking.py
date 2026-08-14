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
    BadSimilaritySettings,
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
    SourcePreferenceGate,
    WeightedEmbedding,
    _compute_neighbor_similarities,
    build_reason_summary,
    compute_bad_similarity_penalty,
    compute_freshness,
    compute_interest_similarity,
    compute_novelty,
    compute_source_article_match,
    compute_source_preference_factor,
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
SOURCE_PREFERENCE_GATE = SourcePreferenceGate(weight_scale=0.15, min_factor=0.5, max_factor=1.5)

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
    source_preference=SOURCE_PREFERENCE_GATE,
)


def make_weighted_embeddings(
    *vectors: tuple[float, ...], weight: float = 1.0
) -> tuple[WeightedEmbedding, ...]:
    """テスト用に、指定した重み（既定 1.0＝全件均等）で `WeightedEmbedding` 群を作る。"""
    return tuple(WeightedEmbedding(vector=vector, weight=weight) for vector in vectors)


EMPTY_PROFILE = InterestProfile(embeddings=(), bad_embeddings=())


def make_candidate(
    *,
    id: uuid.UUID | None = None,
    embedding: tuple[float, ...] | None = (1.0, 0.0, 0.0),
    source_authority: float = 0.5,
    is_primary_source: bool = False,
    source_domain: str = "example.com",
    source_entity_names: tuple[str, ...] = (),
    topics: tuple[str, ...] = (),
    technologies: tuple[str, ...] = (),
    technical_quality: float = 0.5,
    published_at: datetime | None = NOW,
    fetched_at: datetime = NOW,
    duplicate_penalty: float = 0.0,
    is_bad: bool = False,
    is_read: bool = False,
    source_preference: float = 0.0,
) -> CandidateSignature:
    """テスト用の `CandidateSignature` を作る。指定しない項目は無難な既定値にする。"""
    return CandidateSignature(
        id=id or uuid.uuid4(),
        embedding=embedding,
        source_authority=source_authority,
        is_primary_source=is_primary_source,
        source_domain=source_domain,
        source_entity_names=source_entity_names,
        topics=topics,
        technologies=technologies,
        technical_quality=technical_quality,
        published_at=published_at,
        fetched_at=fetched_at,
        duplicate_penalty=duplicate_penalty,
        is_bad=is_bad,
        is_read=is_read,
        source_preference=source_preference,
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
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )
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
            embeddings=make_weighted_embeddings((1.0, 0.0), (0.9, 0.1), (0.0, 1.0)),
            bad_embeddings=(),
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
            embeddings=make_weighted_embeddings((1.0, 0.0), (0.5, 0.5), (0.0, 1.0)),
            bad_embeddings=(),
        )
        candidate = make_candidate(embedding=(0.8, 0.2))

        # Act
        first = compute_interest_similarity(profile, candidate, INTEREST_SETTINGS)
        second = compute_interest_similarity(profile, candidate, INTEREST_SETTINGS)

        # Assert
        assert first == second


class TestComputeInterestSimilarityWeighted:
    """重み付き加重平均への変更を検証する（Issue #15 段階 2）。"""

    def test_matches_the_simple_average_when_all_weights_are_equal(self):
        # Arrange — 退行テスト: 全 weight が等しいとき、単純平均（変更前の挙動）と一致する
        settings = InterestSettings(top_k=2)
        equal_profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0), (0.9, 0.1), (0.0, 1.0), weight=0.5),
            bad_embeddings=(),
        )
        candidate_embedding = (1.0, 0.0)
        candidate = make_candidate(embedding=candidate_embedding)

        # Act
        weighted = compute_interest_similarity(equal_profile, candidate, settings)

        # Assert — 単純平均（上位 2 件の類似度の平均）と一致する
        similarities = sorted(
            (
                cosine_similarity(candidate_embedding, item.vector)
                for item in equal_profile.embeddings
            ),
            reverse=True,
        )[:2]
        simple_average = sum(similarities) / len(similarities)
        assert weighted == pytest.approx(simple_average)

    def test_weighs_a_closer_interest_article_more_when_its_weight_is_larger(self):
        # Arrange — 上位 2 件（(1,0) と (0,1)）のうち、候補に近い (1,0) の重みを
        # 大きくすると、重みを揃えた場合より類似度が高くなる
        settings = InterestSettings(top_k=2)
        candidate = make_candidate(embedding=(1.0, 0.0))
        equal_profile = InterestProfile(
            embeddings=(
                WeightedEmbedding(vector=(1.0, 0.0), weight=1.0),
                WeightedEmbedding(vector=(0.0, 1.0), weight=1.0),
            ),
            bad_embeddings=(),
        )
        skewed_profile = InterestProfile(
            embeddings=(
                WeightedEmbedding(vector=(1.0, 0.0), weight=9.0),
                WeightedEmbedding(vector=(0.0, 1.0), weight=1.0),
            ),
            bad_embeddings=(),
        )

        # Act
        equal_similarity = compute_interest_similarity(equal_profile, candidate, settings)
        skewed_similarity = compute_interest_similarity(skewed_profile, candidate, settings)

        # Assert
        assert skewed_similarity > equal_similarity

    def test_falls_back_to_the_simple_average_when_the_weight_sum_is_zero(self):
        # Arrange — 正負が打ち消し合って重みの合計が 0 になっても 0 除算しない
        settings = InterestSettings(top_k=2)
        profile = InterestProfile(
            embeddings=(
                WeightedEmbedding(vector=(1.0, 0.0), weight=1.0),
                WeightedEmbedding(vector=(0.9, 0.1), weight=-1.0),
            ),
            bad_embeddings=(),
        )
        candidate_embedding = (1.0, 0.0)
        candidate = make_candidate(embedding=candidate_embedding)

        # Act
        similarity = compute_interest_similarity(profile, candidate, settings)

        # Assert — 単純平均にフォールバックする
        similarities = sorted(
            (cosine_similarity(candidate_embedding, item.vector) for item in profile.embeddings),
            reverse=True,
        )[:2]
        simple_average = sum(similarities) / len(similarities)
        assert similarity == pytest.approx(simple_average)


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
    """クラスタ重心距離 × 候補集合内孤立度の新規性を検証する（Issue #89）。

    Issue #87 時点の実装は候補の embedding と関心記事群（`profile.embeddings`）
    との最大コサイン類似度を裏返した値で、`compute_interest_similarity`
    （上位 `top_k` 加重平均）とほぼ完全な相補になっていた（実データでの
    Spearman 順位相関 -0.991）。実データ（関心記事 69 件 / 候補 163 件 /
    クラスタ 8 個）で 6 式を比較し、`cluster_part`（関心クラスタ重心群との
    距離）と `neighbor_part`（採点対象の他候補との孤立度）の min を採用した
    （Spearman -0.687、分布 min 0.094 / p25 0.320 / p50 0.429 / p75 0.501 /
    max 0.738）。`neighbor_part` は `rank_candidates` が
    `_compute_neighbor_similarities` で一括計算し `neighbor_similarity` として
    渡すため、ここでは既に計算済みの値として直接渡す。
    """

    def test_returns_the_default_when_the_candidate_has_no_embedding(self):
        # Arrange
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((1.0, 0.0),))
        candidate = make_candidate(embedding=None)

        # Act / Assert
        assert compute_novelty(candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.9) == (
            NOVELTY_SETTINGS.default_when_no_embedding
        )

    def test_returns_the_default_when_cluster_centroids_is_empty(self):
        # Arrange — クラスタ未構築・記事起点推薦などクラスタ概念が無いプロファイル
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=())
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act / Assert
        assert compute_novelty(candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.9) == (
            NOVELTY_SETTINGS.default_when_no_embedding
        )

    def test_is_higher_the_farther_the_candidate_is_from_the_nearest_cluster_centroid(self):
        # Arrange — neighbor_similarity を揃え、cluster_centroids との距離だけ変える
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((1.0, 0.0),))
        near_candidate = make_candidate(embedding=(1.0, 0.0))
        far_candidate = make_candidate(embedding=(0.0, 1.0))

        # Act
        near_novelty = compute_novelty(
            near_candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.0
        )
        far_novelty = compute_novelty(
            far_candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.0
        )

        # Assert
        assert far_novelty > near_novelty
        assert near_novelty == pytest.approx(0.0)
        assert far_novelty == pytest.approx(1.0)

    def test_is_higher_the_more_isolated_the_candidate_is_within_the_candidate_set(self):
        # Arrange — cluster_centroids を候補と直交させ cluster_part を天井（1.0）に
        # 固定し、neighbor_similarity だけを変える
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((0.0, 1.0),))
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        isolated_novelty = compute_novelty(
            candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.0
        )
        crowded_novelty = compute_novelty(
            candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.99
        )

        # Assert
        assert isolated_novelty > crowded_novelty

    def test_is_the_minimum_of_cluster_part_and_neighbor_part(self):
        # Arrange — cluster_part が低ければ neighbor_part（孤立度）が高くても
        # 全体は低いまま（min であることの固定）
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((1.0, 0.0),))
        candidate = make_candidate(embedding=(1.0, 0.0))  # cluster_part = 1 - 1.0 = 0.0

        # Act — neighbor_similarity=0.0 なら neighbor_part = 1.0（完全に孤立）
        novelty = compute_novelty(candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=0.0)

        # Assert
        assert novelty == pytest.approx(0.0)

    def test_returns_cluster_part_alone_when_there_is_no_other_candidate_to_compare(self):
        # Arrange — 受入基準: 他の候補が無い（採点対象が自分だけ、または他が
        # 全て embedding 無し）とき、既定値 0.5 との min にはならず
        # cluster_part がそのまま返る
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((1.0, 0.0),))
        candidate = make_candidate(embedding=(0.0, 1.0))  # cluster_part = 1 - 0.0 = 1.0

        # Act
        novelty = compute_novelty(candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=None)

        # Assert
        assert novelty == pytest.approx(1.0)

    def test_does_not_exceed_one_when_a_similarity_is_negative(self):
        # Arrange — cluster_part / neighbor_part とも、コサイン類似度が負だと
        # `1 - s` が 1.0 を超えるため clamp されることを固定する
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((1.0, 0.0),))
        candidate = make_candidate(embedding=(-1.0, 0.0))

        # Act / Assert
        assert (
            compute_novelty(candidate, profile, NOVELTY_SETTINGS, neighbor_similarity=-1.0) == 1.0
        )

    def test_interest_similarity_and_novelty_can_vary_independently(self):
        """Issue #89 の回帰テスト。

        Issue #87 時点の実装では novelty が interest_similarity の裏返しで
        あり、interest_similarity を揃えたまま novelty が異なる候補の組を
        作れなかった（実データでの Spearman 順位相関 -0.991）。新式では
        interest_similarity（関心記事群との一致度）と novelty（関心クラスタ
        重心群との距離）が別々のベクトル集合を参照するため、この組を作れる。
        """
        # Arrange — candidate_a / candidate_b は関心記事 (1.0, 0.0) に対して
        # 同じ角度（45度、cos ≈ 0.7071）だが向きが逆で、クラスタ重心
        # （candidate_a と同じ向き）からの距離は大きく異なる
        interest_profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
            cluster_centroids=((0.7071067812, 0.7071067812),),
        )
        candidate_a = make_candidate(embedding=(0.7071067812, 0.7071067812))
        candidate_b = make_candidate(embedding=(0.7071067812, -0.7071067812))

        # Act
        interest_a = compute_interest_similarity(interest_profile, candidate_a, INTEREST_SETTINGS)
        interest_b = compute_interest_similarity(interest_profile, candidate_b, INTEREST_SETTINGS)
        novelty_a = compute_novelty(
            candidate_a, interest_profile, NOVELTY_SETTINGS, neighbor_similarity=None
        )
        novelty_b = compute_novelty(
            candidate_b, interest_profile, NOVELTY_SETTINGS, neighbor_similarity=None
        )

        # Assert — interest_similarity は同じだが novelty は異なる
        assert interest_a == pytest.approx(interest_b, rel=1e-3)
        assert novelty_a == pytest.approx(0.0, abs=1e-3)
        assert novelty_b == pytest.approx(1.0, abs=1e-3)
        assert novelty_a != pytest.approx(novelty_b)


class TestComputeBadSimilarityPenalty:
    """Bad 記事との Embedding 近傍抑制を検証する（`PROJECT_SPEC.md` §7.2、Issue #15 段階 2）。"""

    def test_returns_zero_when_there_are_no_bad_embeddings(self):
        # Arrange
        profile = InterestProfile(embeddings=(), bad_embeddings=())
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act / Assert
        assert compute_bad_similarity_penalty(candidate, profile, BAD_SIMILARITY_SETTINGS) == 0.0

    def test_returns_zero_when_the_candidate_has_no_embedding(self):
        # Arrange
        profile = InterestProfile(embeddings=(), bad_embeddings=((1.0, 0.0),))
        candidate = make_candidate(embedding=None)

        # Act / Assert
        assert compute_bad_similarity_penalty(candidate, profile, BAD_SIMILARITY_SETTINGS) == 0.0

    def test_penalizes_a_candidate_very_close_to_a_bad_article(self):
        # Arrange — Bad 記事とほぼ同一（類似度 1.0）の候補は最大減点になる
        profile = InterestProfile(embeddings=(), bad_embeddings=((1.0, 0.0),))
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        penalty = compute_bad_similarity_penalty(candidate, profile, BAD_SIMILARITY_SETTINGS)

        # Assert
        assert penalty == pytest.approx(BAD_SIMILARITY_SETTINGS.max_penalty)

    def test_does_not_penalize_a_candidate_far_from_bad_articles(self):
        # Arrange — 類似度 0.0（直交）は閾値未満なので減点しない
        profile = InterestProfile(embeddings=(), bad_embeddings=((0.0, 1.0),))
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act / Assert
        assert compute_bad_similarity_penalty(candidate, profile, BAD_SIMILARITY_SETTINGS) == 0.0

    def test_uses_the_maximum_similarity_across_multiple_bad_embeddings(self):
        # Arrange — 複数の Bad embedding のうち最も近いものを基準にする
        profile = InterestProfile(
            embeddings=(),
            bad_embeddings=((0.0, 1.0), (1.0, 0.0)),
        )
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        penalty = compute_bad_similarity_penalty(candidate, profile, BAD_SIMILARITY_SETTINGS)

        # Assert — 最大値（1.0 との類似度）を基準にするため最大減点になる
        assert penalty == pytest.approx(BAD_SIMILARITY_SETTINGS.max_penalty)

    def test_is_zero_exactly_at_the_min_similarity_boundary(self):
        # Arrange — 類似度がちょうど min_similarity（0.7）になるベクトルを作る
        settings = BadSimilaritySettings(min_similarity=0.5, max_penalty=0.5)
        # cos(theta) = 0.5 となる向き
        profile = InterestProfile(embeddings=(), bad_embeddings=((0.5, 0.8660254038),))
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        penalty = compute_bad_similarity_penalty(candidate, profile, settings)

        # Assert
        assert penalty == pytest.approx(0.0, abs=1e-6)

    def test_is_the_max_penalty_at_a_similarity_of_one(self):
        # Arrange
        settings = BadSimilaritySettings(min_similarity=0.5, max_penalty=0.5)
        profile = InterestProfile(embeddings=(), bad_embeddings=((1.0, 0.0),))
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        penalty = compute_bad_similarity_penalty(candidate, profile, settings)

        # Assert
        assert penalty == pytest.approx(settings.max_penalty)

    def test_interpolates_linearly_between_the_boundary_and_one(self):
        # Arrange — min_similarity と 1.0 のちょうど中間の類似度なら減点も中間になる
        settings = BadSimilaritySettings(min_similarity=0.6, max_penalty=1.0)
        # cos(theta) = 0.8 (min_similarity と 1.0 の中間) となる向き
        profile = InterestProfile(embeddings=(), bad_embeddings=((0.8, 0.6),))
        candidate = make_candidate(embedding=(1.0, 0.0))

        # Act
        penalty = compute_bad_similarity_penalty(candidate, profile, settings)

        # Assert
        assert penalty == pytest.approx(0.5, rel=1e-3)


class TestScoreCandidate:
    def test_is_deterministic_for_the_same_input(self):
        # Arrange — 受入基準: 同じ入力に対して常に同じ出力を返す
        candidate = make_candidate(
            embedding=(0.8, 0.2),
            source_authority=0.7,
            topics=("Python",),
            technical_quality=0.6,
        )
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

        # Act
        first = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)
        second = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Assert
        assert first == second

    def test_does_not_raise_when_embedding_and_profile_are_both_missing(self):
        # Arrange — 受入基準: embedding が無い候補・空の関心プロファイルで例外を出さず 0.0
        candidate = make_candidate(embedding=None, topics=())

        # Act
        breakdown = score_candidate(
            candidate, EMPTY_PROFILE, SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert breakdown.interest_similarity == 0.0

    def test_subtracts_the_bad_penalty_when_the_candidate_is_bad(self):
        # Arrange
        good = make_candidate(is_bad=False)
        bad = make_candidate(is_bad=True)
        profile = InterestProfile(embeddings=(), bad_embeddings=())

        # Act
        good_breakdown = score_candidate(good, profile, SETTINGS, NOW, neighbor_similarity=None)
        bad_breakdown = score_candidate(bad, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Assert
        assert bad_breakdown.bad_penalty == PENALTIES.bad
        assert bad_breakdown.total == pytest.approx(good_breakdown.total - PENALTIES.bad)

    def test_subtracts_the_read_penalty_when_the_candidate_is_read(self):
        # Arrange
        unread = make_candidate(is_read=False)
        read = make_candidate(is_read=True)
        profile = InterestProfile(embeddings=(), bad_embeddings=())

        # Act
        unread_breakdown = score_candidate(unread, profile, SETTINGS, NOW, neighbor_similarity=None)
        read_breakdown = score_candidate(read, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Assert
        assert read_breakdown.read_penalty == PENALTIES.read
        assert read_breakdown.total == pytest.approx(unread_breakdown.total - PENALTIES.read)

    def test_subtracts_the_candidates_own_duplicate_penalty_directly(self):
        # Arrange — `articles.duplicate_penalty` をそのまま減点に使う
        original = make_candidate(duplicate_penalty=0.0)
        duplicate = make_candidate(duplicate_penalty=0.6)
        profile = InterestProfile(embeddings=(), bad_embeddings=())

        # Act
        original_breakdown = score_candidate(
            original, profile, SETTINGS, NOW, neighbor_similarity=None
        )
        duplicate_breakdown = score_candidate(
            duplicate, profile, SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert duplicate_breakdown.duplicate_penalty == 0.6
        assert duplicate_breakdown.total == pytest.approx(original_breakdown.total - 0.6)

    def test_subtracts_the_bad_similarity_penalty_when_close_to_a_bad_article(self):
        # Arrange — 受入基準: Bad した記事と Embedding 近傍の候補のスコア（total）が下がる
        near_bad_candidate = make_candidate(embedding=(1.0, 0.0))
        far_from_bad_candidate = make_candidate(embedding=(0.0, 1.0))
        profile = InterestProfile(embeddings=(), bad_embeddings=((1.0, 0.0),))

        # Act
        near_breakdown = score_candidate(
            near_bad_candidate, profile, SETTINGS, NOW, neighbor_similarity=None
        )
        far_breakdown = score_candidate(
            far_from_bad_candidate, profile, SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert near_breakdown.bad_similarity_penalty == pytest.approx(
            BAD_SIMILARITY_SETTINGS.max_penalty
        )
        assert far_breakdown.bad_similarity_penalty == 0.0
        assert near_breakdown.total < far_breakdown.total

    @pytest.mark.parametrize(("raw", "expected"), [(1.5, 1.0), (-0.5, 0.0), (0.5, 0.5)])
    def test_clamps_technical_quality_to_the_unit_range(self, raw: float, expected: float):
        # Arrange
        candidate = make_candidate(technical_quality=raw)
        profile = InterestProfile(embeddings=(), bad_embeddings=())

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)

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
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)
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
            "source_preference_factor",
            "interest_similarity_contribution",
            "source_authority_contribution",
            "source_article_match_contribution",
            "freshness_contribution",
            "technical_quality_contribution",
            "novelty_contribution",
            "bad_penalty",
            "duplicate_penalty",
            "read_penalty",
            "bad_similarity_penalty",
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
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

        # Act
        breakdown = score_candidate(candidate, profile, settings, NOW, neighbor_similarity=None)

        # Assert
        assert breakdown.interest_similarity < 0.0
        assert breakdown.authority_gate_factor == 1.0

    def test_applies_the_full_authority_contribution_when_interest_is_above_the_gate(self):
        # Arrange — interest_similarity が min_interest_similarity 以上ならゲート係数 1.0
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=0.8)
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Assert
        assert breakdown.authority_gate_factor == 1.0
        assert breakdown.source_authority_contribution == pytest.approx(
            0.8 * WEIGHTS.source_authority
        )

    def test_applies_the_minimum_factor_when_interest_similarity_is_zero(self):
        # Arrange
        candidate = make_candidate(embedding=(0.0, 1.0), source_authority=0.8)
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Assert
        assert breakdown.interest_similarity == pytest.approx(0.0)
        assert breakdown.authority_gate_factor == pytest.approx(AUTHORITY_GATE.min_factor)
        assert breakdown.source_authority_contribution == pytest.approx(
            0.8 * WEIGHTS.source_authority * AUTHORITY_GATE.min_factor
        )

    def test_source_preference_does_not_change_the_authority_gate_factor(self):
        # Arrange — 2 つの係数は独立している。情報源選好はゲート係数を動かさない
        candidate = make_candidate(
            embedding=(0.0, 1.0), source_authority=0.8, source_preference=2.0
        )
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

        # Act
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Assert
        assert breakdown.authority_gate_factor == pytest.approx(AUTHORITY_GATE.min_factor)

    def test_interpolates_linearly_between_the_minimum_and_the_gate_threshold(self):
        # Arrange — gate.min_interest_similarity（0.8）の半分の類似度（0.4）なら、
        # 係数も min_factor（0.0）と 1.0 のちょうど中間（0.5）になる
        gate = AuthorityGate(min_interest_similarity=0.8, min_factor=0.0)
        settings = replace(SETTINGS, authority_gate=gate)
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=1.0)
        # 類似度がちょうど 0.4 になるよう、cos(theta)=0.4 の向きにする
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((0.4, 0.9165151390)),
            bad_embeddings=(),
        )

        # Act
        breakdown = score_candidate(candidate, profile, settings, NOW, neighbor_similarity=None)

        # Assert
        assert breakdown.interest_similarity == pytest.approx(0.4, rel=1e-3)
        assert breakdown.authority_gate_factor == pytest.approx(0.5, rel=1e-3)


class TestComputeSourcePreferenceFactor:
    """情報源選好を `source_authority` の寄与に掛ける係数へ変換する（Issue #34）。"""

    def test_returns_one_when_the_user_has_no_preference_for_the_source(self):
        # Arrange / Act — 選好が無い（0.0）情報源は中立
        factor = compute_source_preference_factor(0.0, SOURCE_PREFERENCE_GATE)
        # Assert
        assert factor == pytest.approx(1.0)

    def test_raises_the_factor_for_a_positively_preferred_source(self):
        # Arrange / Act — Good を重ねた情報源（effective_weight が正）
        factor = compute_source_preference_factor(1.6, SOURCE_PREFERENCE_GATE)
        # Assert — 1.0 + 0.15 × 1.6
        assert factor == pytest.approx(1.24)

    def test_lowers_the_factor_for_a_negatively_preferred_source(self):
        # Arrange / Act — Bad が繰り返された情報源（effective_weight が負）
        factor = compute_source_preference_factor(-2.0, SOURCE_PREFERENCE_GATE)
        # Assert — 1.0 - 0.15 × 2.0
        assert factor == pytest.approx(0.7)

    def test_clamps_at_the_maximum_factor(self):
        # Arrange / Act — 選好は累積し続けるため、寄与が青天井にならないよう頭打ちにする
        factor = compute_source_preference_factor(100.0, SOURCE_PREFERENCE_GATE)
        # Assert
        assert factor == pytest.approx(SOURCE_PREFERENCE_GATE.max_factor)

    def test_clamps_at_the_minimum_factor(self):
        # Arrange / Act — 抑制はするが、情報源の権威性を完全にゼロにはしない
        factor = compute_source_preference_factor(-100.0, SOURCE_PREFERENCE_GATE)
        # Assert
        assert factor == pytest.approx(SOURCE_PREFERENCE_GATE.min_factor)


class TestSourcePreferenceInScore:
    """受入基準「情報源選好が推薦スコアへ反映される」（Issue #34）。"""

    def _profile(self) -> InterestProfile:
        return InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )

    def test_a_preferred_source_scores_higher_than_a_neutral_one(self):
        # Arrange
        preferred = make_candidate(
            embedding=(1.0, 0.0), source_authority=0.8, source_preference=2.0
        )
        neutral = make_candidate(embedding=(1.0, 0.0), source_authority=0.8)

        # Act
        preferred_breakdown = score_candidate(
            preferred, self._profile(), SETTINGS, NOW, neighbor_similarity=None
        )
        neutral_breakdown = score_candidate(
            neutral, self._profile(), SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert preferred_breakdown.total > neutral_breakdown.total
        assert preferred_breakdown.source_preference_factor > 1.0

    def test_a_suppressed_source_scores_lower_than_a_neutral_one(self):
        # Arrange
        suppressed = make_candidate(
            embedding=(1.0, 0.0), source_authority=0.8, source_preference=-2.0
        )
        neutral = make_candidate(embedding=(1.0, 0.0), source_authority=0.8)

        # Act
        suppressed_breakdown = score_candidate(
            suppressed, self._profile(), SETTINGS, NOW, neighbor_similarity=None
        )
        neutral_breakdown = score_candidate(
            neutral, self._profile(), SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert suppressed_breakdown.total < neutral_breakdown.total
        assert suppressed_breakdown.source_preference_factor < 1.0

    def test_multiplies_only_the_source_authority_contribution(self):
        # Arrange — 係数は source_authority の寄与だけに掛かり、他項目は動かない
        candidate = make_candidate(
            embedding=(1.0, 0.0),
            source_authority=0.8,
            technical_quality=0.7,
            source_preference=2.0,
        )

        # Act
        breakdown = score_candidate(
            candidate, self._profile(), SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert breakdown.source_preference_factor == pytest.approx(1.3)
        assert breakdown.source_authority_contribution == pytest.approx(
            0.8 * WEIGHTS.source_authority * 1.3
        )
        assert breakdown.technical_quality_contribution == pytest.approx(
            0.7 * WEIGHTS.technical_quality
        )

    def test_no_preference_keeps_the_score_identical_to_the_previous_behavior(self):
        # Arrange — 退行防止: 選好が無ければ係数 1.0 で従来と同じ寄与になる
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=0.8)

        # Act
        breakdown = score_candidate(
            candidate, self._profile(), SETTINGS, NOW, neighbor_similarity=None
        )

        # Assert
        assert breakdown.source_preference_factor == pytest.approx(1.0)
        assert breakdown.source_authority_contribution == pytest.approx(
            0.8 * WEIGHTS.source_authority
        )


class TestRankCandidates:
    def test_is_deterministic_for_the_same_input(self):
        # Arrange — 受入基準: 同じ入力に対して常に同じ出力を返す
        candidates = (
            make_candidate(source_authority=0.9),
            make_candidate(source_authority=0.4),
            make_candidate(source_authority=0.6),
        )
        profile = InterestProfile(embeddings=(), bad_embeddings=())

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
        profile = InterestProfile(embeddings=(), bad_embeddings=())

        # Act
        ranked = rank_candidates((second, first), profile, SETTINGS, NOW)

        # Assert
        assert [c.candidate.id for c in ranked] == [first_id, second_id]

    def test_ranks_the_primary_source_higher_when_interest_similarity_matches_equally(self):
        # Arrange — 受入基準: 関心一致度が同等なら一次情報
        # (is_primary_source=True かつ authority 高) が非公式記事より上位に来る
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )
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
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )
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
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )
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

    def test_lowers_novelty_for_a_candidate_crowded_by_near_duplicates(self):
        # Arrange — 受入基準: rank_candidates 経由で neighbor_part（Issue #89）が
        # 実際に効いている。cluster_centroids を全候補と直交させて cluster_part を
        # 天井（1.0）に固定し、neighbor_part だけで novelty が決まるようにする
        profile = InterestProfile(
            embeddings=(), bad_embeddings=(), cluster_centroids=((0.0, 0.0, 1.0),)
        )
        isolated = make_candidate(embedding=(1.0, 0.0, 0.0))
        duplicate_a = make_candidate(embedding=(0.0, 1.0, 0.0))
        duplicate_b = make_candidate(embedding=(0.0, 1.0, 0.0))

        # Act
        ranked = rank_candidates((isolated, duplicate_a, duplicate_b), profile, SETTINGS, NOW)
        novelty_by_id = {item.candidate.id: item.breakdown.novelty for item in ranked}

        # Assert — 孤立した候補は novelty が高く、集合内で重複した候補は低い
        assert novelty_by_id[isolated.id] == pytest.approx(1.0)
        assert novelty_by_id[duplicate_a.id] == pytest.approx(0.0)
        assert novelty_by_id[duplicate_b.id] == pytest.approx(0.0)

    def test_does_not_raise_when_candidate_embedding_dimensions_differ(self):
        # Arrange — 次元の異なる embedding が混ざっても例外にならず、
        # 次元の合わない候補は最近傍比較の対象から外れる（`_compute_neighbor_similarities`
        # の仕様、Issue #89）。cluster_centroids は次元不一致なら
        # `cosine_similarity` が 0.0 を返す仕様のため cluster_part は 1.0 になる
        profile = InterestProfile(embeddings=(), bad_embeddings=(), cluster_centroids=((0.0, 1.0),))
        normal_a = make_candidate(embedding=(1.0, 0.0))
        normal_b = make_candidate(embedding=(1.0, 0.0))
        odd_dimension = make_candidate(embedding=(1.0, 0.0, 0.0))

        # Act
        ranked = rank_candidates((normal_a, normal_b, odd_dimension), profile, SETTINGS, NOW)
        novelty_by_id = {item.candidate.id: item.breakdown.novelty for item in ranked}

        # Assert
        assert novelty_by_id[odd_dimension.id] == pytest.approx(1.0)


class TestComputeNeighborSimilarities:
    def test_matches_cosine_similarity_for_non_trivial_vectors(self):
        # Arrange — 受入基準: 直交（類似度 0）でも完全一致（類似度 1）でもない、
        # 非自明な値のベクトルで numpy 経由の一括計算を検証する
        # （MR !91 self review、指摘5）。3 件を使い、各要素の「他の候補との
        # 最大類似度」が単一ペアの比較に潰れないようにする
        vector_a = (0.3, 0.7, -0.2, 1.5)
        vector_b = (1.1, -0.4, 0.9, 0.2)
        vector_c = (-0.5, 0.6, 1.2, -0.8)
        vectors = (vector_a, vector_b, vector_c)
        candidates = tuple(make_candidate(embedding=vector) for vector in vectors)

        # Act
        neighbor_similarities = _compute_neighbor_similarities(candidates)

        # Assert — 各要素が純粋関数 cosine_similarity による
        # max(自分以外との類似度) と一致する
        for index, own_vector in enumerate(vectors):
            expected = max(
                cosine_similarity(own_vector, other_vector)
                for other_index, other_vector in enumerate(vectors)
                if other_index != index
            )
            assert neighbor_similarities[index] == pytest.approx(expected)


class TestBuildReasonSummary:
    def test_returns_a_japanese_sentence_citing_the_top_two_contributions(self):
        # Arrange — interest_similarity と source_authority の寄与が最大になるようにする
        candidate = make_candidate(embedding=(1.0, 0.0), source_authority=1.0)
        profile = InterestProfile(
            embeddings=make_weighted_embeddings((1.0, 0.0)),
            bad_embeddings=(),
        )
        breakdown = score_candidate(candidate, profile, SETTINGS, NOW, neighbor_similarity=None)

        # Act
        summary = build_reason_summary(breakdown)

        # Assert
        assert summary.endswith("ため、上位に表示しています。")
        assert "関心との一致度が高く" in summary
        assert "情報源の権威性が高い" in summary
