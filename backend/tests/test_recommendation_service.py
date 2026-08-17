"""推薦の DB 連携と永続化を検証する結合テスト（`PROJECT_SPEC.md` §6.1, §7.1, §13, §14, §15）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db import (
    Article,
    ArticleFeedback,
    Recommendation,
    RecommendationRun,
    SourceRegistry,
    UserArticle,
    UserSourcePreference,
)
from techradar.db.enums import ArticleOrigin, FeedbackAction, RecommendationMode, SourceType
from techradar.recommendation.config import get_scoring_config
from techradar.recommendation.ranking import InterestProfile
from techradar.recommendation.service import (
    FeedFilters,
    build_interest_profile,
    compute_filter_fingerprint,
    count_recommendations,
    find_latest_run,
    generate_recommendations,
    load_candidates,
    load_recommendation_page,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)
EMBEDDING_DIM = 1024


class _Unset:
    """`published_at` の「未指定」を表す番兵（`test_dedup_service.py` と同じ考え方）。"""


_UNSET_PUBLISHED_AT = _Unset()


def make_embedding(active_index: int = 0) -> list[float]:
    """1 箇所だけ 1.0 を立てたベクトルを返す。

    同じ `active_index` の embedding 同士はコサイン類似度 1.0（完全一致）、
    異なる `active_index` 同士は 0.0（無関係）になるため、関心一致度を
    テストで制御しやすい。
    """
    vector = [0.0] * EMBEDDING_DIM
    vector[active_index] = 1.0
    return vector


def make_article(
    session: Session,
    *,
    title: str = "記事タイトル",
    translated_title: str | None = None,
    summary_ja: str | None = None,
    source_domain: str = "example.com",
    source_authority: float = 0.5,
    technical_quality: float = 0.5,
    is_primary_source: bool = False,
    topics: Sequence[str] = (),
    technologies: Sequence[str] = (),
    published_at: datetime | _Unset | None = _UNSET_PUBLISHED_AT,
    fetched_at: datetime | None = None,
    is_dead: bool = False,
    embedding: list[float] | None = None,
    duplicate_penalty: float = 0.0,
) -> Article:
    """推薦候補として使う記事を DB へ保存する。"""
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    resolved_published_at = NOW if isinstance(published_at, _Unset) else published_at
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title=title,
        translated_title=translated_title,
        summary_ja=summary_ja,
        source_domain=source_domain,
        source_authority=source_authority,
        technical_quality=technical_quality,
        is_primary_source=is_primary_source,
        topics=list(topics),
        technologies=list(technologies),
        published_at=resolved_published_at,
        fetched_at=fetched_at or NOW,
        is_dead=is_dead,
        embedding=embedding,
        duplicate_penalty=duplicate_penalty,
    )
    session.add(article)
    session.flush()
    return article


def add_user_article(
    session: Session,
    user_id: uuid.UUID,
    article: Article,
    origin: ArticleOrigin,
    *,
    interest_weight: float = 1.0,
    created_at: datetime | None = None,
) -> UserArticle:
    """関心記事登録（`user_articles`）を作る。"""
    row = UserArticle(
        user_id=user_id,
        article_id=article.id,
        origin=origin.value,
        interest_weight=interest_weight,
        created_at=created_at or NOW,
    )
    session.add(row)
    session.flush()
    return row


def add_feedback(
    session: Session,
    user_id: uuid.UUID,
    article: Article,
    action: FeedbackAction,
    *,
    created_at: datetime | None = None,
) -> ArticleFeedback:
    """フィードバック（`article_feedback`）を作る。"""
    row = ArticleFeedback(
        user_id=user_id,
        article_id=article.id,
        action=action.value,
        created_at=created_at or NOW,
    )
    session.add(row)
    session.flush()
    return row


def add_source_registry(
    session: Session,
    *,
    entity_name: str,
    domain: str,
    github_org: str | None = None,
    source_type: SourceType = SourceType.OFFICIAL_BLOG,
    authority_score: float = 0.9,
) -> SourceRegistry:
    """公式ソースレジストリ（`source_registry`）を作る。"""
    row = SourceRegistry(
        entity_name=entity_name,
        domain=domain,
        github_org=github_org,
        source_type=source_type.value,
        authority_score=authority_score,
        verified=True,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def settings() -> Settings:
    """`.env` に依存しない既定値の設定（`test_fetcher_service.py` と同じ方針）。"""
    return Settings(_env_file=None)


@pytest.fixture(autouse=True)
def _reset_scoring_config_cache() -> Iterator[None]:
    """テスト間で `get_scoring_config` のキャッシュが汚染されないようにする。"""
    get_scoring_config.cache_clear()
    yield
    get_scoring_config.cache_clear()


class TestLoadCandidatesExcludesBadArticles:
    def test_excludes_an_article_the_user_marked_bad(self, db_session: Session, settings: Settings):
        # Arrange — 受入基準: Bad 済み記事がフィードに出現しない
        user_id = uuid.uuid4()
        bad_article = make_article(db_session, title="Bad済み記事")
        add_feedback(db_session, user_id, bad_article, FeedbackAction.BAD)
        good_article = make_article(db_session, title="通常記事")

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert bad_article.id not in candidate_ids
        assert good_article.id in candidate_ids


class TestLoadCandidatesMarksReadArticles:
    def test_keeps_a_read_article_but_marks_it_as_read(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 既読記事は候補に残るが is_read=True になる（除外ではなく減点）
        user_id = uuid.uuid4()
        read_article = make_article(db_session, title="全文閲覧済み記事")
        add_user_article(db_session, user_id, read_article, ArticleOrigin.READ_FULL)
        clicked_article = make_article(db_session, title="クリック済み記事")
        add_user_article(db_session, user_id, clicked_article, ArticleOrigin.CLICKED)
        unread_article = make_article(db_session, title="未読記事")

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)
        by_id = {candidate.id: candidate for candidate in candidates}

        # Assert
        assert by_id[read_article.id].is_read is True
        assert by_id[clicked_article.id].is_read is True
        assert by_id[unread_article.id].is_read is False


class TestLoadCandidatesExcludesOwnedArticles:
    @pytest.mark.parametrize(
        "origin", [ArticleOrigin.MANUAL, ArticleOrigin.GOOD, ArticleOrigin.SAVED]
    )
    def test_excludes_an_article_already_registered_as_an_interest_article(
        self, db_session: Session, settings: Settings, origin: ArticleOrigin
    ):
        # Arrange — 既に自分のものになっている記事は Discover に再掲しない
        user_id = uuid.uuid4()
        owned_article = make_article(db_session, title="登録済み記事")
        add_user_article(db_session, user_id, owned_article, origin)
        other_article = make_article(db_session, title="別記事")

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert owned_article.id not in candidate_ids
        assert other_article.id in candidate_ids


class TestLoadCandidatesFiltersByAge:
    def test_excludes_an_article_published_more_than_the_max_age_days_ago(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 7 日より古い記事が候補に入らない
        user_id = uuid.uuid4()
        old_article = make_article(
            db_session, title="古い記事", published_at=NOW - timedelta(days=8)
        )
        fresh_article = make_article(
            db_session, title="新しい記事", published_at=NOW - timedelta(days=1)
        )

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert old_article.id not in candidate_ids
        assert fresh_article.id in candidate_ids

    def test_uses_fetched_at_when_published_at_is_null(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — published_at が NULL の記事は fetched_at で判定する
        user_id = uuid.uuid4()
        old_undated = make_article(
            db_session,
            title="古い日付未取得記事",
            published_at=None,
            fetched_at=NOW - timedelta(days=8),
        )
        fresh_undated = make_article(
            db_session,
            title="新しい日付未取得記事",
            published_at=None,
            fetched_at=NOW - timedelta(days=1),
        )

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert old_undated.id not in candidate_ids
        assert fresh_undated.id in candidate_ids


class TestLoadCandidatesExcludesDeadArticles:
    def test_excludes_a_dead_article(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        dead_article = make_article(db_session, title="削除済み記事", is_dead=True)
        alive_article = make_article(db_session, title="生存記事")

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert dead_article.id not in candidate_ids
        assert alive_article.id in candidate_ids


class TestLoadCandidatesExcludesSourceArticle:
    def test_excludes_the_source_article_itself_for_article_based_mode(
        self, db_session: Session, settings: Settings
    ):
        # Arrange
        user_id = uuid.uuid4()
        source_article = make_article(db_session, title="起点記事")
        other_article = make_article(db_session, title="別記事")

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, source_article_id=source_article.id
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert source_article.id not in candidate_ids
        assert other_article.id in candidate_ids


class TestLoadCandidatesResolvesSourceEntityNames:
    def test_collects_entity_names_and_github_org_from_the_source_registry(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: source_entity_names が source_registry から引けている
        user_id = uuid.uuid4()
        article = make_article(db_session, title="公式記事", source_domain="anthropic.com")
        add_source_registry(
            db_session,
            entity_name="Anthropic",
            domain="anthropic.com",
            github_org="anthropics",
        )
        unrelated_article = make_article(
            db_session, title="無関係記事", source_domain="example.com"
        )

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)
        by_id = {candidate.id: candidate for candidate in candidates}

        # Assert
        assert set(by_id[article.id].source_entity_names) == {"Anthropic", "anthropics"}
        assert by_id[unrelated_article.id].source_entity_names == ()


class TestLoadCandidatesResolvesSourcePreferences:
    """受入基準: 学習済みの情報源選好が採点対象（`CandidateSignature`）へ載る（Issue #34）。"""

    def test_attaches_the_users_preference_for_the_articles_source(
        self, db_session: Session, settings: Settings
    ):
        # Arrange
        user_id = uuid.uuid4()
        preferred = make_article(db_session, title="好みの情報源", source_domain="blog.example.jp")
        neutral = make_article(db_session, title="選好なし", source_domain="other.example.jp")
        db_session.add(
            UserSourcePreference(
                user_id=user_id,
                source_domain="blog.example.jp",
                positive_weight=1.6,
                negative_weight=0.0,
                effective_weight=1.6,
                updated_at=NOW,
            )
        )
        db_session.flush()

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)
        by_id = {candidate.id: candidate for candidate in candidates}

        # Assert — 行が無いドメインは中立（0.0）
        assert by_id[preferred.id].source_preference == pytest.approx(1.6)
        assert by_id[neutral.id].source_preference == pytest.approx(0.0)

    def test_ignores_another_users_preference(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        article = make_article(db_session, source_domain="blog.example.jp")
        db_session.add(
            UserSourcePreference(
                user_id=other_user_id,
                source_domain="blog.example.jp",
                positive_weight=1.6,
                negative_weight=0.0,
                effective_weight=1.6,
                updated_at=NOW,
            )
        )
        db_session.flush()

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings)
        by_id = {candidate.id: candidate for candidate in candidates}

        # Assert
        assert by_id[article.id].source_preference == pytest.approx(0.0)


class TestLoadCandidatesDeterministicOrder:
    def test_returns_the_same_order_across_two_calls(self, db_session: Session, settings: Settings):
        # Arrange — 受入基準: 同じ入力で 2 回実行しても順位が同じ
        user_id = uuid.uuid4()
        for index in range(5):
            make_article(
                db_session,
                title=f"記事{index}",
                published_at=NOW - timedelta(hours=index),
            )

        # Act
        first = load_candidates(db_session, user_id, NOW, settings)
        second = load_candidates(db_session, user_id, NOW, settings)

        # Assert
        assert [candidate.id for candidate in first] == [candidate.id for candidate in second]


class TestBuildInterestProfile:
    def test_collects_embeddings_from_interest_articles(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 関心プロファイルが embedding を集められている
        user_id = uuid.uuid4()
        manual_article = make_article(
            db_session, title="手動登録記事", embedding=make_embedding(0), topics=["llm"]
        )
        add_user_article(db_session, user_id, manual_article, ArticleOrigin.MANUAL)
        good_feedback_article = make_article(
            db_session, title="Good記事", embedding=make_embedding(1), topics=["rag"]
        )
        add_feedback(db_session, user_id, good_feedback_article, FeedbackAction.GOOD)
        no_embedding_article = make_article(
            db_session, title="embedding無し記事", embedding=None, topics=["agent"]
        )
        add_user_article(db_session, user_id, no_embedding_article, ArticleOrigin.SAVED)

        # Act
        profile = build_interest_profile(db_session, user_id, NOW, settings)

        # Assert — embedding が無い記事は無視され、他 2 件の embedding は集まる
        assert len(profile.embeddings) == 2
        assert all(item.weight > 0.0 for item in profile.embeddings)

    def test_returns_an_empty_profile_without_raising_when_there_are_no_interest_articles(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 関心記事ゼロでも例外にならない
        user_id = uuid.uuid4()

        # Act
        profile = build_interest_profile(db_session, user_id, NOW, settings)

        # Assert
        assert profile == InterestProfile(embeddings=(), bad_embeddings=())

    def test_truncates_to_the_configured_limit_and_logs_a_warning(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — interest.max_profile_articles を 1 に絞り、打ち切りを検証する
        config = get_scoring_config()
        limited_config = config.model_copy(
            update={"interest": config.interest.model_copy(update={"max_profile_articles": 1})}
        )
        # 対象記事の読み込み・重み計算は interest.service.load_weighted_interest_articles
        # へ委譲した（DRY、rebuild_interest_clusters と共有）ため、その呼び出し元の
        # get_scoring_config / logger を差し替える。
        monkeypatch.setattr("techradar.interest.service.get_scoring_config", lambda: limited_config)
        user_id = uuid.uuid4()
        older_article = make_article(db_session, title="古い関心記事", embedding=make_embedding(0))
        add_user_article(
            db_session,
            user_id,
            older_article,
            ArticleOrigin.MANUAL,
            created_at=NOW - timedelta(days=1),
        )
        newer_article = make_article(
            db_session, title="新しい関心記事", embedding=make_embedding(1)
        )
        add_user_article(db_session, user_id, newer_article, ArticleOrigin.MANUAL, created_at=NOW)
        messages: list[str] = []
        monkeypatch.setattr(
            "techradar.interest.service.logger.warning",
            lambda message, *args, **kwargs: messages.append(message % args if args else message),
        )

        # Act
        profile = build_interest_profile(db_session, user_id, NOW, settings)

        # Assert — 新しい順に残るため newer_article の embedding だけが残る
        assert [item.vector for item in profile.embeddings] == [tuple(make_embedding(1))]
        assert any("truncated_count=1" in message for message in messages)

    def test_weighs_a_recent_good_article_more_than_an_older_one(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 時間減衰により古い Good より新しい Good の
        # weight が大きい。origin は両方 good に揃え、created_at の差だけにする
        user_id = uuid.uuid4()
        old_article = make_article(db_session, title="古いGood記事", embedding=make_embedding(0))
        add_user_article(
            db_session,
            user_id,
            old_article,
            ArticleOrigin.GOOD,
            created_at=NOW - timedelta(days=60),
        )
        new_article = make_article(db_session, title="新しいGood記事", embedding=make_embedding(1))
        add_user_article(db_session, user_id, new_article, ArticleOrigin.GOOD, created_at=NOW)

        # Act
        profile = build_interest_profile(db_session, user_id, NOW, settings)
        weight_by_vector = {item.vector: item.weight for item in profile.embeddings}

        # Assert
        assert (
            weight_by_vector[tuple(make_embedding(1))] > weight_by_vector[tuple(make_embedding(0))]
        )

    def test_weighs_a_manual_registration_more_than_a_click(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: origin=manual の記事は origin=clicked の記事より
        # weight が大きい（explicit_weight の差）。created_at は揃えて時間減衰の
        # 影響を排除する
        user_id = uuid.uuid4()
        manual_article = make_article(db_session, title="手動登録記事", embedding=make_embedding(0))
        add_user_article(db_session, user_id, manual_article, ArticleOrigin.MANUAL, created_at=NOW)
        clicked_article = make_article(
            db_session, title="クリック記事", embedding=make_embedding(1)
        )
        add_user_article(
            db_session, user_id, clicked_article, ArticleOrigin.CLICKED, created_at=NOW
        )

        # Act
        profile = build_interest_profile(db_session, user_id, NOW, settings)
        weight_by_vector = {item.vector: item.weight for item in profile.embeddings}

        # Assert
        assert (
            weight_by_vector[tuple(make_embedding(0))] > weight_by_vector[tuple(make_embedding(1))]
        )

    def test_collects_bad_article_embeddings_into_bad_embeddings(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: Bad した記事の embedding が bad_embeddings に入る
        user_id = uuid.uuid4()
        bad_article = make_article(db_session, title="Bad記事", embedding=make_embedding(0))
        add_feedback(db_session, user_id, bad_article, FeedbackAction.BAD)

        # Act
        profile = build_interest_profile(db_session, user_id, NOW, settings)

        # Assert
        assert profile.bad_embeddings == (tuple(make_embedding(0)),)


class TestGenerateRecommendationsDiscover:
    def test_saves_a_run_and_ranked_recommendations_with_reasons(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: スコア内訳が DB (recommendations.reasons) で確認できる
        user_id = uuid.uuid4()
        interest_article = make_article(
            db_session, title="関心記事", embedding=make_embedding(0), topics=["llm"]
        )
        add_user_article(db_session, user_id, interest_article, ArticleOrigin.GOOD)
        make_article(db_session, title="近い候補記事", embedding=make_embedding(0), topics=["llm"])
        make_article(db_session, title="遠い候補記事", embedding=make_embedding(5), topics=["css"])

        # Act
        result = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Assert
        run = db_session.get(RecommendationRun, result.run_id)
        assert run is not None
        assert run.user_id == user_id
        assert run.mode == RecommendationMode.DISCOVER.value
        assert run.source_article_id is None

        saved = db_session.scalars(
            select(Recommendation)
            .where(Recommendation.run_id == result.run_id)
            .order_by(Recommendation.rank)
        ).all()
        assert len(saved) == len(result.items) == 2
        assert [row.rank for row in saved] == list(range(1, len(saved) + 1))
        for row in saved:
            assert "summary" in row.reasons
            assert "interest_similarity" in row.reasons
            assert "total" in row.reasons

    def test_produces_the_same_ranking_when_run_twice(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 同じ入力で 2 回実行しても順位が同じ
        user_id = uuid.uuid4()
        interest_article = make_article(
            db_session, title="関心記事", embedding=make_embedding(0), topics=["llm"]
        )
        add_user_article(db_session, user_id, interest_article, ArticleOrigin.GOOD)
        for index in range(3):
            make_article(
                db_session, title=f"候補{index}", embedding=make_embedding(index), topics=["llm"]
            )

        # Act
        first = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )
        second = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Assert
        first_order = [item.candidate.id for item in first.items]
        second_order = [item.candidate.id for item in second.items]
        assert first_order == second_order


class TestGenerateRecommendationsArticleBased:
    def test_excludes_the_source_article_from_its_own_recommendations(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 起点記事自身が推薦に含まれない
        user_id = uuid.uuid4()
        source_article = make_article(
            db_session, title="起点記事", embedding=make_embedding(0), topics=["llm"]
        )
        related_article = make_article(
            db_session, title="関連記事", embedding=make_embedding(0), topics=["llm"]
        )

        # Act
        result = generate_recommendations(
            db_session,
            user_id,
            RecommendationMode.ARTICLE_BASED,
            settings,
            NOW,
            source_article_id=source_article.id,
        )

        # Assert
        run = db_session.get(RecommendationRun, result.run_id)
        assert run is not None
        assert run.source_article_id == source_article.id
        item_ids = {item.candidate.id for item in result.items}
        assert source_article.id not in item_ids
        assert related_article.id in item_ids

    def test_novelty_differs_between_candidates_near_and_far_from_the_source(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 起点記事を唯一の中心として novelty に差が付く（Issue #89）。
        # `cluster_centroids` を空のままにすると `compute_novelty` が既定値を返し、
        # 記事起点推薦の novelty が候補によらず一律になる
        user_id = uuid.uuid4()
        source_article = make_article(
            db_session, title="起点記事", embedding=make_embedding(0), topics=["llm"]
        )
        near_article = make_article(
            db_session, title="起点に近い記事", embedding=make_embedding(0), topics=["llm"]
        )
        far_article = make_article(
            db_session, title="起点から遠い記事", embedding=make_embedding(5), topics=["db"]
        )

        # Act
        result = generate_recommendations(
            db_session,
            user_id,
            RecommendationMode.ARTICLE_BASED,
            settings,
            NOW,
            source_article_id=source_article.id,
        )

        # Assert
        novelty_by_article_id = {item.candidate.id: item.breakdown.novelty for item in result.items}
        assert novelty_by_article_id[near_article.id] < novelty_by_article_id[far_article.id]

    def test_works_when_the_source_article_has_no_embedding(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 起点記事に embedding が無くても空プロファイルで動く
        user_id = uuid.uuid4()
        source_article = make_article(db_session, title="起点記事", embedding=None)
        other_article = make_article(db_session, title="候補記事", embedding=make_embedding(0))

        # Act
        result = generate_recommendations(
            db_session,
            user_id,
            RecommendationMode.ARTICLE_BASED,
            settings,
            NOW,
            source_article_id=source_article.id,
        )

        # Assert
        item_ids = {item.candidate.id for item in result.items}
        assert other_article.id in item_ids


class TestLoadRecommendationPage:
    def test_returns_the_next_page_without_duplicates_using_after_rank(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: after_rank で重複なく次ページを返す
        user_id = uuid.uuid4()
        add_user_article(
            db_session,
            user_id,
            make_article(db_session, title="関心記事", embedding=make_embedding(0)),
            ArticleOrigin.GOOD,
        )
        for index in range(5):
            make_article(
                db_session, title=f"候補{index}", embedding=make_embedding(index), topics=["llm"]
            )
        result = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Act
        first_page = load_recommendation_page(db_session, result.run_id, limit=2)
        second_page = load_recommendation_page(
            db_session, result.run_id, after_rank=first_page[-1][0].rank, limit=2
        )

        # Assert
        first_ids = {recommendation.article_id for recommendation, _ in first_page}
        second_ids = {recommendation.article_id for recommendation, _ in second_page}
        assert len(first_page) == 2
        assert len(second_page) == 2
        assert first_ids.isdisjoint(second_ids)
        assert [recommendation.rank for recommendation, _ in first_page] == [1, 2]
        assert [recommendation.rank for recommendation, _ in second_page] == [3, 4]


class TestFindLatestRun:
    def test_returns_the_most_recently_generated_run_for_the_mode(
        self, db_session: Session, settings: Settings
    ):
        # Arrange
        user_id = uuid.uuid4()
        make_article(db_session, title="候補", embedding=make_embedding(0))
        older = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW - timedelta(hours=1)
        )
        newer = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Act
        latest = find_latest_run(db_session, user_id, RecommendationMode.DISCOVER)

        # Assert
        assert latest is not None
        assert latest.id == newer.run_id
        assert latest.id != older.run_id

    def test_returns_none_when_the_user_has_no_runs(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()

        # Act
        latest = find_latest_run(db_session, user_id, RecommendationMode.DISCOVER)

        # Assert
        assert latest is None

    def test_filters_by_fingerprint_when_given(self, db_session: Session, settings: Settings):
        """受入基準: 条件が違えば別 run とみなす（Issue #90）。

        同じ user・mode でも `filter_fingerprint` が異なる run は
        `find_latest_run` の対象から外れる。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_article(db_session, title="候補")
        filters_a = FeedFilters(query="python")
        filters_b = FeedFilters(query="rust")
        run_a = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW, feed_filters=filters_a
        )

        # Act
        latest_for_a = find_latest_run(
            db_session,
            user_id,
            RecommendationMode.DISCOVER,
            fingerprint=compute_filter_fingerprint(filters_a),
        )
        latest_for_b = find_latest_run(
            db_session,
            user_id,
            RecommendationMode.DISCOVER,
            fingerprint=compute_filter_fingerprint(filters_b),
        )

        # Assert
        assert latest_for_a is not None
        assert latest_for_a.id == run_a.run_id
        assert latest_for_b is None


class TestComputeFilterFingerprint:
    """`compute_filter_fingerprint` の正規化を検証する（Issue #90）。"""

    def test_same_conditions_produce_the_same_fingerprint(self) -> None:
        # Arrange
        first = FeedFilters(query="LLM", topics=("llm", "rag"), technologies=("python",))
        second = FeedFilters(query="llm", topics=("llm", "rag"), technologies=("python",))

        # Act / Assert — 検索語は大文字小文字を区別しないため同一視する
        assert compute_filter_fingerprint(first) == compute_filter_fingerprint(second)

    def test_topic_order_does_not_affect_the_fingerprint(self) -> None:
        # Arrange — 受入基準: 順序だけが違う指定は同じフィンガープリントになる
        ordered_ab = FeedFilters(topics=("a", "b"))
        ordered_ba = FeedFilters(topics=("b", "a"))

        # Act / Assert
        assert compute_filter_fingerprint(ordered_ab) == compute_filter_fingerprint(ordered_ba)

    def test_different_conditions_produce_different_fingerprints(self) -> None:
        # Arrange
        with_query = FeedFilters(query="python")
        without_query = FeedFilters()

        # Act / Assert
        assert compute_filter_fingerprint(with_query) != compute_filter_fingerprint(without_query)

    def test_technology_order_does_not_affect_the_fingerprint(self) -> None:
        # Arrange
        ordered_ab = FeedFilters(technologies=("go", "rust"))
        ordered_ba = FeedFilters(technologies=("rust", "go"))

        # Act / Assert
        assert compute_filter_fingerprint(ordered_ab) == compute_filter_fingerprint(ordered_ba)

    def test_different_max_age_days_produce_different_fingerprints(self) -> None:
        # Arrange — 対象期間が違えば別条件なので run を使い回してはいけない
        seven_days = FeedFilters(max_age_days=7)
        thirty_days = FeedFilters(max_age_days=30)

        # Act / Assert
        assert compute_filter_fingerprint(seven_days) != compute_filter_fingerprint(thirty_days)

    def test_the_same_instant_in_another_offset_produces_the_same_fingerprint(self) -> None:
        # Arrange — 同じ時刻をUTCとJSTで表しただけの指定は同一条件として扱う
        in_utc = FeedFilters(published_from=datetime(2026, 8, 1, tzinfo=UTC))
        in_jst = FeedFilters(
            published_from=datetime(2026, 8, 1, 9, tzinfo=timezone(timedelta(hours=9)))
        )

        # Act / Assert
        assert compute_filter_fingerprint(in_utc) == compute_filter_fingerprint(in_jst)


class TestLoadCandidatesFiltersBySearchQuery:
    """受入基準: 検索語が title/translated_title/summary_ja のいずれかに部分一致すれば当たる。"""

    def test_matches_the_title_case_insensitively(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        target = make_article(db_session, title="Python入門ガイド")
        other = make_article(db_session, title="Rustハンドブック")

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, feed_filters=FeedFilters(query="python")
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert target.id in candidate_ids
        assert other.id not in candidate_ids

    def test_matches_the_translated_title(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        target = make_article(db_session, title="Original Title", translated_title="日本語タイトル")
        other = make_article(db_session, title="Other Title", translated_title="別の見出し")

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, feed_filters=FeedFilters(query="日本語")
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert target.id in candidate_ids
        assert other.id not in candidate_ids

    def test_matches_the_summary(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        target = make_article(db_session, title="記事A", summary_ja="LLMの活用事例を紹介する")
        other = make_article(db_session, title="記事B", summary_ja="CSSレイアウトの基礎")

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, feed_filters=FeedFilters(query="LLM")
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert target.id in candidate_ids
        assert other.id not in candidate_ids


class TestLoadCandidatesFiltersByTopicsAndTechnologies:
    """受入基準: topics/technologies を複数指定したとき、全てを含む記事だけが残る（AND）。"""

    def test_requires_all_specified_topics(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        both = make_article(db_session, title="両方持ち", topics=["llm", "rag"])
        only_one = make_article(db_session, title="片方だけ", topics=["llm"])

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, feed_filters=FeedFilters(topics=("llm", "rag"))
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert both.id in candidate_ids
        assert only_one.id not in candidate_ids

    def test_requires_all_specified_technologies(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        both = make_article(db_session, title="両方持ち", technologies=["python", "fastapi"])
        only_one = make_article(db_session, title="片方だけ", technologies=["python"])

        # Act
        candidates = load_candidates(
            db_session,
            user_id,
            NOW,
            settings,
            feed_filters=FeedFilters(technologies=("python", "fastapi")),
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert both.id in candidate_ids
        assert only_one.id not in candidate_ids


class TestLoadCandidatesFiltersByPublishedRangeAndSourceDomain:
    """受入基準: 公開日の範囲、情報源ドメインで絞れる。"""

    def test_filters_by_published_at_range(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        in_range = make_article(db_session, title="範囲内", published_at=NOW - timedelta(days=1))
        too_old = make_article(db_session, title="範囲外", published_at=NOW - timedelta(days=5))

        # Act
        candidates = load_candidates(
            db_session,
            user_id,
            NOW,
            settings,
            feed_filters=FeedFilters(published_from=NOW - timedelta(days=2), published_to=NOW),
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert in_range.id in candidate_ids
        assert too_old.id not in candidate_ids

    def test_uses_fetched_at_when_published_at_is_null(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 公開日欠損時は fetched_at を代替に使う既存ロジックに揃える
        user_id = uuid.uuid4()
        target = make_article(
            db_session, title="fetched_atで代替", published_at=None, fetched_at=NOW
        )

        # Act
        candidates = load_candidates(
            db_session,
            user_id,
            NOW,
            settings,
            feed_filters=FeedFilters(published_from=NOW - timedelta(days=1), published_to=NOW),
        )

        # Assert
        assert target.id in {candidate.id for candidate in candidates}

    def test_filters_by_source_domain(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        target = make_article(db_session, title="対象ドメイン", source_domain="example.com")
        other = make_article(db_session, title="別ドメイン", source_domain="other.example")

        # Act
        candidates = load_candidates(
            db_session,
            user_id,
            NOW,
            settings,
            feed_filters=FeedFilters(source_domain="example.com"),
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert target.id in candidate_ids
        assert other.id not in candidate_ids


class TestLoadCandidatesFiltersByMaxAgeDays:
    """受入基準: 対象期間を1〜180日で指定でき、freshness スコアには影響しない（Issue #90）。"""

    def test_includes_articles_older_than_the_configured_window(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準そのもの。既定の7日では候補に入らない記事が、
        # 対象期間を広げると入る（自己レビューで判明した「黙って0件」の解消）
        user_id = uuid.uuid4()
        old_article = make_article(
            db_session, title="30日前の記事", published_at=NOW - timedelta(days=30)
        )

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, feed_filters=FeedFilters(max_age_days=60)
        )

        # Assert
        assert old_article.id in {candidate.id for candidate in candidates}

    def test_excludes_articles_older_than_the_given_max_age_days(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 指定した期間の境界を跨ぐデータで確認する
        user_id = uuid.uuid4()
        within = make_article(db_session, title="期間内", published_at=NOW - timedelta(days=2))
        beyond = make_article(db_session, title="期間外", published_at=NOW - timedelta(days=4))

        # Act
        candidates = load_candidates(
            db_session, user_id, NOW, settings, feed_filters=FeedFilters(max_age_days=3)
        )

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert within.id in candidate_ids
        assert beyond.id not in candidate_ids

    def test_falls_back_to_the_configured_window_when_not_given(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 未指定なら従来どおり scoring.yaml の freshness.max_age_days（7日）
        user_id = uuid.uuid4()
        old_article = make_article(
            db_session, title="8日前の記事", published_at=NOW - timedelta(days=8)
        )
        fresh_article = make_article(
            db_session, title="1日前の記事", published_at=NOW - timedelta(days=1)
        )

        # Act
        candidates = load_candidates(db_session, user_id, NOW, settings, feed_filters=FeedFilters())

        # Assert
        candidate_ids = {candidate.id for candidate in candidates}
        assert old_article.id not in candidate_ids
        assert fresh_article.id in candidate_ids

    def test_does_not_change_the_freshness_score(self, db_session: Session, settings: Settings):
        # Arrange — 受入基準: 期間の選び方で同じ記事のスコアが変わらない
        # （減衰基準は scoring.yaml の freshness.max_age_days のままにしてある）
        user_id = uuid.uuid4()
        article = make_article(
            db_session,
            title="対象期間の内側の記事",
            published_at=NOW - timedelta(days=3),
            embedding=make_embedding(0),
        )

        # Act — 対象期間だけを変えて2回生成する
        default_run = generate_recommendations(
            db_session,
            user_id,
            RecommendationMode.DISCOVER,
            settings,
            NOW,
            feed_filters=FeedFilters(max_age_days=7),
        )
        widened_run = generate_recommendations(
            db_session,
            user_id,
            RecommendationMode.DISCOVER,
            settings,
            NOW,
            feed_filters=FeedFilters(max_age_days=180),
        )

        # Assert
        default_freshness = _freshness_of(db_session, default_run.run_id, article.id)
        widened_freshness = _freshness_of(db_session, widened_run.run_id, article.id)
        assert default_freshness == widened_freshness


def _freshness_of(session: Session, run_id: uuid.UUID, article_id: uuid.UUID) -> float:
    """指定 run の指定記事について、reasons に保存された freshness の素の値を返す。"""
    reasons = session.scalar(
        select(Recommendation.reasons).where(
            Recommendation.run_id == run_id, Recommendation.article_id == article_id
        )
    )
    assert reasons is not None
    return float(reasons["freshness"])


class TestLoadRecommendationPageOffset:
    """`offset` によるページ番号ベースのページングを検証する（Issue #90）。"""

    def test_returns_pages_by_offset_without_duplicates(
        self, db_session: Session, settings: Settings
    ):
        # Arrange
        user_id = uuid.uuid4()
        for index in range(5):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))
        result = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Act
        first_page = load_recommendation_page(db_session, result.run_id, offset=0, limit=2)
        second_page = load_recommendation_page(db_session, result.run_id, offset=2, limit=2)

        # Assert
        first_ids = {recommendation.article_id for recommendation, _ in first_page}
        second_ids = {recommendation.article_id for recommendation, _ in second_page}
        assert len(first_page) == 2
        assert len(second_page) == 2
        assert first_ids.isdisjoint(second_ids)
        assert [recommendation.rank for recommendation, _ in first_page] == [1, 2]
        assert [recommendation.rank for recommendation, _ in second_page] == [3, 4]

    def test_returns_empty_for_an_out_of_range_offset(
        self, db_session: Session, settings: Settings
    ):
        # Arrange — 受入基準: 範囲外のページ番号はエラーではなく空
        user_id = uuid.uuid4()
        make_article(db_session, title="候補", embedding=make_embedding(0))
        result = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Act
        page = load_recommendation_page(db_session, result.run_id, offset=100, limit=20)

        # Assert
        assert page == ()


class TestCountRecommendations:
    """`count_recommendations`（総件数、Issue #90）を検証する。"""

    def test_counts_the_rows_stored_for_the_run(self, db_session: Session, settings: Settings):
        # Arrange
        user_id = uuid.uuid4()
        for index in range(3):
            make_article(db_session, title=f"候補{index}", embedding=make_embedding(index))
        result = generate_recommendations(
            db_session, user_id, RecommendationMode.DISCOVER, settings, NOW
        )

        # Act
        total = count_recommendations(db_session, result.run_id)

        # Assert
        assert total == 3

    def test_returns_zero_for_an_unknown_run(self, db_session: Session) -> None:
        # Act
        total = count_recommendations(db_session, uuid.uuid4())

        # Assert
        assert total == 0
