"""記事の重複判定ロジックを検証する（`PROJECT_SPEC.md` §17）。

判定は純粋関数として実装するため、DB を使わずに検証できる。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from techradar.db.enums import ContentType, SourceType
from techradar.dedup.rules import (
    MAX_COMPARISON_TITLE_CHARACTERS,
    ArticleCluster,
    ArticleSignature,
    DuplicateMatch,
    DuplicatePenalties,
    DuplicateThresholds,
    MatchMethod,
    UniqueValueSettings,
    _levenshtein_distance,
    cluster_articles,
    cosine_similarity,
    duplicate_penalty_for,
    find_duplicate_match,
    normalize_title,
    select_representative,
    title_similarity,
    unique_value_candidates,
)
from techradar.embedding.fake import FakeEmbeddingProvider

THRESHOLDS = DuplicateThresholds(title_similarity=0.90, embedding_similarity=0.92)
PENALTIES = DuplicatePenalties(
    canonical_url=1.0, normalized_url=1.0, body_hash=1.0, title=0.8, embedding=0.6
)
UNIQUE_VALUE_SETTINGS = UniqueValueSettings(
    content_types=(ContentType.IMPLEMENTATION, ContentType.RESEARCH),
    min_technical_quality=0.70,
    max_authority_gap=0.30,
    max_candidates_per_cluster=2,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def make_signature(
    *,
    id: uuid.UUID | None = None,
    canonical_url: str = "https://example.com/a",
    original_url: str | None = None,
    title: str = "記事のタイトル",
    body_hash: str | None = None,
    embedding: tuple[float, ...] | None = None,
    source_authority: float = 0.5,
    source_type: SourceType = SourceType.UNKNOWN,
    content_type: ContentType | None = None,
    technical_quality: float = 0.5,
    published_at: datetime | None = BASE_TIME,
) -> ArticleSignature:
    """テスト用の `ArticleSignature` を作る。指定しない項目は無難な既定値にする。"""
    return ArticleSignature(
        id=id or uuid.uuid4(),
        canonical_url=canonical_url,
        original_url=original_url or canonical_url,
        title=title,
        body_hash=body_hash,
        embedding=embedding,
        source_authority=source_authority,
        source_type=source_type,
        content_type=content_type,
        technical_quality=technical_quality,
        published_at=published_at,
    )


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # 全角・半角の違い
            ("Claude Code入門", "Ｃｌａｕｄｅ Ｃｏｄｅ入門"),
            # 大文字・小文字の違い
            ("GPT-5 Released", "gpt-5 released"),
            # 句読点・空白の有無
            ("新しいAPI、公開！", "新しいAPI公開"),
        ],
    )
    def test_normalizes_representation_differences_to_the_same_string(self, left: str, right: str):
        # Arrange / Act / Assert — 表記ゆれで判定が変わらないこと
        assert normalize_title(left) == normalize_title(right)

    def test_returns_empty_string_for_a_title_made_only_of_symbols(self):
        # Arrange / Act / Assert
        assert normalize_title("・・・！？") == ""

    def test_truncates_titles_longer_than_the_comparison_limit(self):
        # Arrange — 上限文字数より後ろの違いは比較に影響しない
        shared_prefix = "あ" * MAX_COMPARISON_TITLE_CHARACTERS
        left = shared_prefix + "この先の違いは無視されるはずその1"
        right = shared_prefix + "この先の違いは無視されるはずその2"

        # Act / Assert
        assert normalize_title(left) == normalize_title(right)
        assert len(normalize_title(left)) <= MAX_COMPARISON_TITLE_CHARACTERS


class TestLevenshteinDistance:
    """`title_similarity` が使う編集距離の自前実装（外部依存を増やさないため）。"""

    def test_returns_zero_for_identical_strings(self):
        # Arrange / Act / Assert
        assert _levenshtein_distance("abc", "abc") == 0

    def test_returns_the_length_of_the_other_string_when_one_side_is_empty(self):
        # Arrange / Act / Assert
        assert _levenshtein_distance("", "abc") == 3
        assert _levenshtein_distance("abc", "") == 3

    def test_counts_a_single_substitution(self):
        # Arrange / Act / Assert
        assert _levenshtein_distance("cat", "cot") == 1

    def test_counts_insertions_and_deletions(self):
        # Arrange / Act / Assert — kitten -> sitting は古典的な例（距離 3）
        assert _levenshtein_distance("kitten", "sitting") == 3


class TestTitleSimilarity:
    def test_returns_one_for_identical_titles(self):
        # Arrange / Act / Assert
        assert title_similarity("GPT-5を試した", "GPT-5を試した") == 1.0

    def test_returns_a_high_score_for_a_reposted_title_with_minor_edits(self):
        # Arrange / Act — 転載でよくある「【翻訳】」「まとめ」の付与
        score = title_similarity("Claude Codeで始めるAI開発", "【翻訳】Claude Codeで始めるAI開発")

        # Assert
        assert score >= 0.90

    def test_returns_a_low_score_for_unrelated_titles(self):
        # Arrange / Act
        score = title_similarity("Pythonの新機能", "量子コンピュータ入門")

        # Assert
        assert score < 0.5

    @pytest.mark.parametrize(("left", "right"), [("", ""), ("タイトル", ""), ("", "タイトル")])
    def test_returns_zero_when_either_title_is_empty_after_normalization(
        self, left: str, right: str
    ):
        # Arrange / Act / Assert — 無題の記事同士を重複にしない
        assert title_similarity(left, right) == 0.0


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


class TestFindDuplicateMatch:
    def test_does_not_compare_a_signature_with_itself(self):
        # Arrange
        signature = make_signature()

        # Act / Assert — 同じ id 同士は比較しない
        assert find_duplicate_match(signature, signature, THRESHOLDS) is None

    def test_returns_none_when_nothing_matches(self):
        # Arrange
        left = make_signature(canonical_url="https://a.example/1", title="全く違う話題のタイトル")
        right = make_signature(canonical_url="https://b.example/2", title="Completely Unrelated")

        # Act / Assert
        assert find_duplicate_match(left, right, THRESHOLDS) is None

    def test_does_not_treat_two_articles_with_a_blank_canonical_url_as_duplicates(self):
        # Arrange — canonical_url は NOT NULL だが空文字は入りうる。
        # 空文字同士の一致を「重複」と扱うと、canonical 抽出に失敗した無関係な
        # 記事同士がすべて重複扱いになってしまう。
        left = make_signature(
            canonical_url="",
            original_url="",
            title="全く違う話題のタイトルその一",
        )
        right = make_signature(
            canonical_url="",
            original_url="",
            title="Completely Unrelated Topic Two",
        )

        # Act / Assert
        assert find_duplicate_match(left, right, THRESHOLDS) is None

    def test_matches_on_body_hash_when_urls_and_titles_differ(self):
        # Arrange — URL・タイトルは異なるが本文ハッシュが一致する（ミラー掲載など）
        left = make_signature(
            canonical_url="https://a.example/1", title="タイトルA", body_hash="deadbeef"
        )
        right = make_signature(
            canonical_url="https://b.example/2", title="タイトルB", body_hash="deadbeef"
        )

        # Act
        match = find_duplicate_match(left, right, THRESHOLDS)

        # Assert
        assert match == DuplicateMatch(method=MatchMethod.BODY_HASH, similarity=1.0)

    def test_matches_on_embedding_similarity_when_nothing_else_matches(self):
        # Arrange — 手書きの短いベクトルで意図的にほぼ同じ方向を作る
        left = make_signature(
            canonical_url="https://a.example/1",
            title="タイトルA",
            embedding=(1.0, 0.0, 0.0),
        )
        right = make_signature(
            canonical_url="https://b.example/2",
            title="タイトルB",
            embedding=(0.99, 0.01, 0.0),
        )

        # Act
        match = find_duplicate_match(left, right, THRESHOLDS)

        # Assert
        assert match is not None
        assert match.method == MatchMethod.EMBEDDING
        assert match.similarity >= THRESHOLDS.embedding_similarity

    def test_does_not_match_on_embedding_for_unrelated_articles(self):
        # Arrange — FakeEmbeddingProvider は異なる文字列をほぼ直交させる
        provider = FakeEmbeddingProvider(dimensions=16)
        vectors = provider.embed_documents(["記事Aの本文", "全く別の話題の本文"])
        left = make_signature(
            canonical_url="https://a.example/1",
            title="記事Aのタイトル",
            embedding=tuple(vectors[0]),
        )
        right = make_signature(
            canonical_url="https://b.example/2",
            title="別のタイトル",
            embedding=tuple(vectors[1]),
        )

        # Act / Assert
        assert find_duplicate_match(left, right, THRESHOLDS) is None


class TestAcceptanceCriteria:
    def test_eliminates_a_duplicate_with_an_identical_canonical_url(self):
        # Arrange — 受入基準: canonical URL 一致の重複が排除される
        left = make_signature(canonical_url="https://example.com/article", title="記事A")
        right = make_signature(canonical_url="https://example.com/article", title="記事B")

        # Act
        match = find_duplicate_match(left, right, THRESHOLDS)

        # Assert
        assert match == DuplicateMatch(method=MatchMethod.CANONICAL_URL, similarity=1.0)

    def test_treats_the_same_article_with_different_tracking_parameters_as_duplicate(self):
        # Arrange — 受入基準: トラッキングパラメータ違いの同一記事が重複と判定される
        left = make_signature(
            canonical_url="https://example.com/article?utm_source=twitter",
            original_url="https://example.com/article?utm_source=twitter",
            title="記事A",
        )
        right = make_signature(
            canonical_url="https://example.com/article?utm_source=newsletter",
            original_url="https://example.com/article?utm_source=newsletter",
            title="記事A",
        )

        # Act
        match = find_duplicate_match(left, right, THRESHOLDS)

        # Assert — canonical_url 文字列自体は異なるので正規化 URL の段で一致する
        assert match is not None
        assert match.method == MatchMethod.NORMALIZED_URL

    def test_treats_a_repost_with_an_almost_identical_title_as_duplicate(self):
        # Arrange — 受入基準: タイトルがほぼ同一の転載記事が重複と判定される
        # 「【転載】」の付与という軽微な差が、タイトル全体の長さに対して閾値
        # (0.90) を超えるだけの短い差になるよう、十分な長さのタイトルにする
        left = make_signature(
            canonical_url="https://official.example/post",
            title="アンソロピックが新しいAIモデルを発表しました",
        )
        right = make_signature(
            canonical_url="https://repost.example/mirror/post",
            title="［転載］アンソロピックが新しいAIモデルを発表しました",
        )

        # Act
        match = find_duplicate_match(left, right, THRESHOLDS)

        # Assert
        assert match is not None
        assert match.method == MatchMethod.TITLE

    def test_selects_the_official_article_as_representative_over_a_repost(self):
        # Arrange — 受入基準: 公式記事と転載記事のクラスタで公式が代表に選ばれる
        official = make_signature(
            canonical_url="https://official.example/post",
            title="新しいモデルを発表しました",
            source_authority=0.90,
            source_type=SourceType.OFFICIAL_BLOG,
        )
        repost = make_signature(
            canonical_url="https://repost.example/mirror/post",
            title="【転載】新しいモデルを発表しました",
            source_authority=0.20,
            source_type=SourceType.NEWS_REPOST,
        )
        cluster = ArticleCluster(members=(repost, official), matches=())

        # Act
        representative = select_representative(cluster)

        # Assert
        assert representative is official

    def test_keeps_an_analysis_article_as_a_unique_value_candidate_but_drops_a_plain_repost(
        self,
    ):
        # Arrange — 受入基準: 独自検証を含みうる解説記事は候補に残り、単なる転載は残らない
        official = make_signature(
            canonical_url="https://official.example/post",
            title="新しいモデルを発表しました",
            source_authority=0.90,
            source_type=SourceType.OFFICIAL_BLOG,
        )
        analysis = make_signature(
            canonical_url="https://blogger.example/analysis",
            title="新しいモデルを実測してみた",
            source_authority=0.60,
            source_type=SourceType.PERSONAL_ARTICLE,
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.85,
        )
        plain_repost = make_signature(
            canonical_url="https://repost.example/mirror/post",
            title="【転載】新しいモデルを発表しました",
            source_authority=0.20,
            source_type=SourceType.NEWS_REPOST,
            content_type=ContentType.NEWS,
            technical_quality=0.30,
        )
        cluster = ArticleCluster(members=(official, analysis, plain_repost), matches=())

        # Act
        candidates = unique_value_candidates(cluster, official, UNIQUE_VALUE_SETTINGS)

        # Assert
        assert candidates == (analysis,)


class TestClusterArticles:
    def test_transitively_links_articles_into_a_single_cluster(self):
        # Arrange — a-b はタイトル一致、b-c は canonical URL 一致。推移的に 1 クラスタにする
        a = make_signature(canonical_url="https://a.example/1", title="発表内容の詳細")
        b = make_signature(canonical_url="https://b.example/2", title="発表内容の詳細！")
        c = make_signature(canonical_url="https://b.example/2", title="全く別のタイトルC")

        # Act
        clusters = cluster_articles((a, b, c), THRESHOLDS)

        # Assert
        assert len(clusters) == 1
        assert {member.id for member in clusters[0].members} == {a.id, b.id, c.id}

    def test_keeps_unrelated_articles_in_separate_clusters(self):
        # Arrange
        a = make_signature(canonical_url="https://a.example/1", title="トピックA")
        b = make_signature(canonical_url="https://b.example/2", title="トピックB")

        # Act
        clusters = cluster_articles((a, b), THRESHOLDS)

        # Assert
        assert len(clusters) == 2

    def test_returns_a_singleton_cluster_for_a_lone_article(self):
        # Arrange
        only = make_signature()

        # Act
        clusters = cluster_articles((only,), THRESHOLDS)

        # Assert
        assert clusters == (ArticleCluster(members=(only,), matches=()),)


class TestSelectRepresentative:
    def test_selects_the_signature_with_the_highest_authority(self):
        # Arrange
        low = make_signature(source_authority=0.4)
        high = make_signature(source_authority=0.9)
        cluster = ArticleCluster(members=(low, high), matches=())

        # Act / Assert
        assert select_representative(cluster) is high

    def test_prefers_a_primary_source_when_authority_ties(self):
        # Arrange
        secondary = make_signature(source_authority=0.6, source_type=SourceType.COMPANY_TECH_BLOG)
        primary = make_signature(
            source_authority=0.6, source_type=SourceType.OFFICIAL_DOCUMENTATION
        )
        cluster = ArticleCluster(members=(secondary, primary), matches=())

        # Act / Assert
        assert select_representative(cluster) is primary

    def test_prefers_the_earlier_published_article_when_authority_and_tier_tie(self):
        # Arrange — 原典が先に出るため、古い方を優先する
        later = make_signature(
            source_authority=0.6,
            source_type=SourceType.OFFICIAL_BLOG,
            published_at=BASE_TIME + timedelta(days=1),
        )
        earlier = make_signature(
            source_authority=0.6, source_type=SourceType.OFFICIAL_BLOG, published_at=BASE_TIME
        )
        cluster = ArticleCluster(members=(later, earlier), matches=())

        # Act / Assert
        assert select_representative(cluster) is earlier

    def test_sorts_a_missing_published_at_last(self):
        # Arrange
        undated = make_signature(
            source_authority=0.6, source_type=SourceType.OFFICIAL_BLOG, published_at=None
        )
        dated = make_signature(
            source_authority=0.6, source_type=SourceType.OFFICIAL_BLOG, published_at=BASE_TIME
        )
        cluster = ArticleCluster(members=(undated, dated), matches=())

        # Act / Assert
        assert select_representative(cluster) is dated

    def test_breaks_a_full_tie_by_id_string_order(self):
        # Arrange — 決定的にするための最終手段
        first_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        second_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        second = make_signature(
            id=second_id,
            source_authority=0.6,
            source_type=SourceType.OFFICIAL_BLOG,
            published_at=BASE_TIME,
        )
        first = make_signature(
            id=first_id,
            source_authority=0.6,
            source_type=SourceType.OFFICIAL_BLOG,
            published_at=BASE_TIME,
        )
        cluster = ArticleCluster(members=(second, first), matches=())

        # Act / Assert
        assert select_representative(cluster) is first


class TestDuplicatePenaltyFor:
    @pytest.mark.parametrize(
        ("method", "expected"),
        [
            (MatchMethod.CANONICAL_URL, PENALTIES.canonical_url),
            (MatchMethod.NORMALIZED_URL, PENALTIES.normalized_url),
            (MatchMethod.BODY_HASH, PENALTIES.body_hash),
            (MatchMethod.TITLE, PENALTIES.title),
            (MatchMethod.EMBEDDING, PENALTIES.embedding),
        ],
    )
    def test_returns_the_penalty_configured_for_the_matched_method(
        self, method: MatchMethod, expected: float
    ):
        # Arrange
        match = DuplicateMatch(method=method, similarity=1.0)

        # Act / Assert
        assert duplicate_penalty_for(match, PENALTIES) == expected


class TestUniqueValueCandidates:
    def test_excludes_the_representative_itself(self):
        # Arrange
        representative = make_signature(
            content_type=ContentType.IMPLEMENTATION, technical_quality=0.9, source_authority=0.9
        )
        cluster = ArticleCluster(members=(representative,), matches=())

        # Act / Assert
        assert unique_value_candidates(cluster, representative, UNIQUE_VALUE_SETTINGS) == ()

    def test_excludes_a_content_type_outside_the_allow_list(self):
        # Arrange
        representative = make_signature(source_authority=0.9)
        other = make_signature(
            content_type=ContentType.NEWS, technical_quality=0.9, source_authority=0.8
        )
        cluster = ArticleCluster(members=(representative, other), matches=())

        # Act / Assert
        assert unique_value_candidates(cluster, representative, UNIQUE_VALUE_SETTINGS) == ()

    def test_excludes_a_technical_quality_below_the_threshold(self):
        # Arrange
        representative = make_signature(source_authority=0.9)
        other = make_signature(
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.1,
            source_authority=0.8,
        )
        cluster = ArticleCluster(members=(representative, other), matches=())

        # Act / Assert
        assert unique_value_candidates(cluster, representative, UNIQUE_VALUE_SETTINGS) == ()

    def test_excludes_an_authority_gap_beyond_the_configured_maximum(self):
        # Arrange
        representative = make_signature(source_authority=0.9)
        other = make_signature(
            content_type=ContentType.IMPLEMENTATION,
            technical_quality=0.9,
            source_authority=0.1,
        )
        cluster = ArticleCluster(members=(representative, other), matches=())

        # Act / Assert
        assert unique_value_candidates(cluster, representative, UNIQUE_VALUE_SETTINGS) == ()

    def test_limits_candidates_to_the_configured_maximum_ordered_by_quality(self):
        # Arrange — 3 件の候補があるが上限は 2 件
        representative = make_signature(source_authority=0.9)
        best = make_signature(
            content_type=ContentType.IMPLEMENTATION, technical_quality=0.95, source_authority=0.8
        )
        middle = make_signature(
            content_type=ContentType.RESEARCH, technical_quality=0.85, source_authority=0.8
        )
        worst = make_signature(
            content_type=ContentType.IMPLEMENTATION, technical_quality=0.75, source_authority=0.8
        )
        cluster = ArticleCluster(members=(worst, representative, best, middle), matches=())

        # Act
        candidates = unique_value_candidates(cluster, representative, UNIQUE_VALUE_SETTINGS)

        # Assert
        assert candidates == (best, middle)

    def test_selects_the_same_candidates_regardless_of_input_order_when_quality_ties(self):
        # Arrange — 受入基準（冪等性）: technical_quality が同点の候補が上限を
        # 跨ぐ場合、`_target_articles` の行順（SQL が保証しない）に依存せず、
        # id の文字列順という決定的な二次キーで毎回同じ候補が選ばれる
        representative = make_signature(source_authority=0.9)
        tied_settings = UniqueValueSettings(
            content_types=(ContentType.IMPLEMENTATION,),
            min_technical_quality=0.0,
            max_authority_gap=1.0,
            max_candidates_per_cluster=2,
        )
        tied_candidates = [
            make_signature(
                content_type=ContentType.IMPLEMENTATION,
                technical_quality=0.80,
                source_authority=0.8,
            )
            for _ in range(4)
        ]
        cluster_in_order = ArticleCluster(members=(representative, *tied_candidates), matches=())
        cluster_reversed = ArticleCluster(
            members=(representative, *reversed(tied_candidates)), matches=()
        )

        # Act
        result_in_order = unique_value_candidates(cluster_in_order, representative, tied_settings)
        result_reversed = unique_value_candidates(cluster_reversed, representative, tied_settings)

        # Assert
        assert result_in_order == result_reversed
        expected_ids = tuple(sorted((c.id for c in tied_candidates), key=str)[:2])
        assert tuple(candidate.id for candidate in result_in_order) == expected_ids
