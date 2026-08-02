"""`interest/service.py`（DB 連携）を検証する結合テスト。

`PROJECT_SPEC.md` §7, §8, Issue #15 段階 3。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from techradar.db.enums import ArticleOrigin, FeedbackAction
from techradar.db.models import (
    Article,
    ArticleFeedback,
    UserArticle,
    UserInterestCluster,
    UserTopicPreference,
)
from techradar.interest.service import (
    load_weighted_interest_articles,
    rebuild_interest_clusters,
    update_topic_preferences,
)
from techradar.recommendation.config import get_scoring_config

NOW = datetime(2026, 8, 1, tzinfo=UTC)
EMBEDDING_DIM = 1024


def make_embedding(active_index: int = 0) -> list[float]:
    """1 箇所だけ 1.0 を立てたベクトルを返す（`test_recommendation_service.py` と同じ考え方）。"""
    vector = [0.0] * EMBEDDING_DIM
    vector[active_index] = 1.0
    return vector


def make_article(
    session: Session,
    *,
    title: str = "記事タイトル",
    topics: Sequence[str] = (),
    embedding: list[float] | None = None,
) -> Article:
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title=title,
        source_domain="example.com",
        topics=list(topics),
        embedding=embedding,
        fetched_at=NOW,
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
    created_at: datetime | None = None,
) -> UserArticle:
    row = UserArticle(
        user_id=user_id,
        article_id=article.id,
        origin=origin.value,
        interest_weight=1.0,
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
    row = ArticleFeedback(
        user_id=user_id,
        article_id=article.id,
        action=action.value,
        created_at=created_at or NOW,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture(autouse=True)
def _reset_scoring_config_cache() -> Iterator[None]:
    get_scoring_config.cache_clear()
    yield
    get_scoring_config.cache_clear()


def _get_topic_preference(
    session: Session, user_id: uuid.UUID, topic: str
) -> UserTopicPreference | None:
    return session.get(UserTopicPreference, (user_id, topic))


class TestUpdateTopicPreferencesGoodAndSave:
    def test_good_increases_positive_and_effective_weight(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["llm"])

        # Act
        update_topic_preferences(db_session, user_id, article.id, FeedbackAction.GOOD, NOW)

        # Assert — 増分は config/scoring.yaml の feedback_weights.good
        config = get_scoring_config()
        preference = _get_topic_preference(db_session, user_id, "llm")
        assert preference is not None
        assert preference.positive_weight == pytest.approx(config.feedback_weights.good)
        assert preference.effective_weight > 0.0

    def test_save_increases_positive_weight_by_the_save_increment(
        self, db_session: Session
    ) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["rag"])

        # Act
        update_topic_preferences(db_session, user_id, article.id, FeedbackAction.SAVE, NOW)

        # Assert
        config = get_scoring_config()
        preference = _get_topic_preference(db_session, user_id, "rag")
        assert preference is not None
        assert preference.positive_weight == pytest.approx(config.feedback_weights.save)

    def test_good_updates_every_topic_of_the_article(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["llm", "rag"])

        # Act
        update_topic_preferences(db_session, user_id, article.id, FeedbackAction.GOOD, NOW)

        # Assert
        assert _get_topic_preference(db_session, user_id, "llm") is not None
        assert _get_topic_preference(db_session, user_id, "rag") is not None

    def test_repeated_good_accumulates_the_positive_weight(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["llm"])
        update_topic_preferences(db_session, user_id, article.id, FeedbackAction.GOOD, NOW)

        # Act — 別記事でも同じトピックへの Good を重ねる
        another = make_article(db_session, title="別記事", topics=["llm"])
        update_topic_preferences(db_session, user_id, another.id, FeedbackAction.GOOD, NOW)

        # Assert
        config = get_scoring_config()
        preference = _get_topic_preference(db_session, user_id, "llm")
        assert preference is not None
        assert preference.positive_weight == pytest.approx(config.feedback_weights.good * 2)

    def test_does_nothing_when_the_article_has_no_topics(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=[])

        # Act
        update_topic_preferences(db_session, user_id, article.id, FeedbackAction.GOOD, NOW)

        # Assert — 例外にならず、行も作られない
        assert (
            db_session.scalar(
                select(UserTopicPreference).where(UserTopicPreference.user_id == user_id)
            )
            is None
        )

    def test_does_nothing_when_the_article_does_not_exist(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()

        # Act / Assert — 例外にならない
        update_topic_preferences(db_session, user_id, uuid.uuid4(), FeedbackAction.GOOD, NOW)


class TestUpdateTopicPreferencesBad:
    """受入基準: 単発の Bad では下がらず、直近5記事中3記事以上が Bad になった時点で下がる。"""

    def test_a_single_bad_does_not_lower_the_topic_weight(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["llm"])

        # Act
        update_topic_preferences(db_session, user_id, article.id, FeedbackAction.BAD, NOW)

        # Assert — 閾値未達のため行すら作られない
        assert _get_topic_preference(db_session, user_id, "llm") is None

    def test_lowers_the_weight_once_three_of_five_recent_are_bad(self, db_session: Session) -> None:
        # Arrange — 同一トピック「llm」の記事を5件作り、Good/Good/Bad/Bad の状態から
        # 3件目の Bad を送って閾値（3/5）に達させる
        user_id = uuid.uuid4()
        good_1 = make_article(db_session, title="good-1", topics=["llm"])
        add_feedback(
            db_session, user_id, good_1, FeedbackAction.GOOD, created_at=NOW - timedelta(days=4)
        )
        good_2 = make_article(db_session, title="good-2", topics=["llm"])
        add_feedback(
            db_session, user_id, good_2, FeedbackAction.GOOD, created_at=NOW - timedelta(days=3)
        )
        bad_1 = make_article(db_session, title="bad-1", topics=["llm"])
        add_feedback(
            db_session, user_id, bad_1, FeedbackAction.BAD, created_at=NOW - timedelta(days=2)
        )
        bad_2 = make_article(db_session, title="bad-2", topics=["llm"])
        add_feedback(
            db_session, user_id, bad_2, FeedbackAction.BAD, created_at=NOW - timedelta(days=1)
        )
        target = make_article(db_session, title="bad-3", topics=["llm"])
        add_feedback(db_session, user_id, target, FeedbackAction.BAD, created_at=NOW)

        # Act
        update_topic_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)

        # Assert
        config = get_scoring_config()
        preference = _get_topic_preference(db_session, user_id, "llm")
        assert preference is not None
        assert preference.negative_weight == pytest.approx(config.topic_preference.decay_step)

    def test_ignores_bad_feedback_outside_the_recent_window(self, db_session: Session) -> None:
        # Arrange — 直近5件より前の Bad は数えない（直近5件中は Good ばかり）
        user_id = uuid.uuid4()
        for index in range(3):
            old_bad = make_article(db_session, title=f"old-bad-{index}", topics=["llm"])
            add_feedback(
                db_session,
                user_id,
                old_bad,
                FeedbackAction.BAD,
                created_at=NOW - timedelta(days=10 + index),
            )
        for index in range(4):
            recent_good = make_article(db_session, title=f"recent-good-{index}", topics=["llm"])
            add_feedback(
                db_session,
                user_id,
                recent_good,
                FeedbackAction.GOOD,
                created_at=NOW - timedelta(days=3 - index),
            )
        target = make_article(db_session, title="target", topics=["llm"])
        add_feedback(db_session, user_id, target, FeedbackAction.BAD, created_at=NOW)

        # Act
        update_topic_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)

        # Assert — 直近5件中 Bad は 1 件（target 自身）だけのため下がらない
        assert _get_topic_preference(db_session, user_id, "llm") is None


class TestLoadWeightedInterestArticles:
    def test_collects_embeddings_and_weights(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["llm"], embedding=make_embedding(0))
        add_user_article(db_session, user_id, article, ArticleOrigin.MANUAL)

        # Act
        records = load_weighted_interest_articles(db_session, user_id, NOW)

        # Assert
        assert len(records) == 1
        assert records[0].embedding == tuple(make_embedding(0))
        assert records[0].topics == ("llm",)
        assert records[0].weight > 0.0

    def test_returns_empty_for_a_user_without_interest_articles(self, db_session: Session) -> None:
        # Arrange / Act
        records = load_weighted_interest_articles(db_session, uuid.uuid4(), NOW)
        # Assert
        assert records == ()


class TestRebuildInterestClusters:
    """受入基準: 関心記事からクラスタが生成され、weight 合計が 1.0 に正規化される。

    再実行で置き換わる。
    """

    def test_builds_clusters_whose_weights_sum_to_one(self, db_session: Session) -> None:
        # Arrange — 明確に離れた2群の関心記事を作る
        user_id = uuid.uuid4()
        for index in range(4):
            article = make_article(
                db_session, title=f"ai-{index}", topics=["AI"], embedding=make_embedding(0)
            )
            add_user_article(db_session, user_id, article, ArticleOrigin.GOOD)
        for index in range(4):
            article = make_article(
                db_session, title=f"devops-{index}", topics=["DevOps"], embedding=make_embedding(1)
            )
            add_user_article(db_session, user_id, article, ArticleOrigin.GOOD)

        # Act
        count = rebuild_interest_clusters(db_session, user_id, NOW)

        # Assert
        clusters = db_session.scalars(
            select(UserInterestCluster).where(UserInterestCluster.user_id == user_id)
        ).all()
        assert count == len(clusters)
        assert count >= 2
        assert sum(cluster.weight for cluster in clusters) == pytest.approx(1.0, abs=1e-9)
        assert all(cluster.centroid_embedding is not None for cluster in clusters)

    def test_rerun_replaces_old_clusters_instead_of_accumulating(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["AI"], embedding=make_embedding(0))
        add_user_article(db_session, user_id, article, ArticleOrigin.GOOD)
        first_count = rebuild_interest_clusters(db_session, user_id, NOW)
        assert first_count >= 1

        # Act — 対象記事は変わらないまま再実行する
        second_count = rebuild_interest_clusters(db_session, user_id, NOW)

        # Assert — 古い行が積み増しされず、同じ件数のまま置き換わる
        clusters = db_session.scalars(
            select(UserInterestCluster).where(UserInterestCluster.user_id == user_id)
        ).all()
        assert len(clusters) == second_count
        assert second_count == first_count

    def test_returns_zero_and_clears_old_clusters_when_no_interest_articles_remain(
        self, db_session: Session
    ) -> None:
        # Arrange — 一度クラスタを作ってから対象記事を持たない状態にする
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["AI"], embedding=make_embedding(0))
        add_user_article(db_session, user_id, article, ArticleOrigin.GOOD)
        rebuild_interest_clusters(db_session, user_id, NOW)
        db_session.execute(delete(UserArticle).where(UserArticle.user_id == user_id))
        db_session.flush()

        # Act
        count = rebuild_interest_clusters(db_session, user_id, NOW)

        # Assert
        assert count == 0
        assert (
            db_session.scalar(
                select(UserInterestCluster).where(UserInterestCluster.user_id == user_id)
            )
            is None
        )

    def test_does_not_touch_another_users_clusters(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        other_article = make_article(
            db_session, title="other", topics=["AI"], embedding=make_embedding(0)
        )
        add_user_article(db_session, other_user_id, other_article, ArticleOrigin.GOOD)
        rebuild_interest_clusters(db_session, other_user_id, NOW)

        # Act — 対象記事を持たない別ユーザーで再構築する
        count = rebuild_interest_clusters(db_session, user_id, NOW)

        # Assert
        assert count == 0
        other_clusters = db_session.scalars(
            select(UserInterestCluster).where(UserInterestCluster.user_id == other_user_id)
        ).all()
        assert len(other_clusters) >= 1
