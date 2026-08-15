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

from techradar.db.enums import ArticleOrigin, FeedbackAction, JobStatus
from techradar.db.models import (
    Article,
    ArticleFeedback,
    UserArticle,
    UserInterestCluster,
    UserSourcePreference,
    UserTopicPreference,
)
from techradar.interest.service import (
    count_interest_articles_by_origin,
    load_weighted_interest_articles,
    rebuild_interest_clusters,
    recompute_source_preferences_after_removal,
    recompute_topic_preferences_after_removal,
    update_source_preferences,
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
    source_domain: str = "example.com",
    analysis_status: str | None = JobStatus.COMPLETED.value,
) -> Article:
    canonical_url = f"https://{source_domain}/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title=title,
        source_domain=source_domain,
        topics=list(topics),
        embedding=embedding,
        analysis_status=analysis_status,
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


def _get_source_preference(
    session: Session, user_id: uuid.UUID, source_domain: str
) -> UserSourcePreference | None:
    return session.get(UserSourcePreference, (user_id, source_domain))


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


class TestRecomputeTopicPreferencesAfterRemoval:
    """`recompute_topic_preferences_after_removal`（Issue #15 自己レビュー 1）を検証する。

    呼び出し側の想定どおり、対象の `article_feedback` 行を削除した「後」に
    呼ぶ（`recompute_topic_preferences_after_removal` の docstring 参照）。
    """

    def test_lowers_negative_weight_once_removal_drops_below_the_threshold(
        self, db_session: Session
    ) -> None:
        # Arrange — 直近5件中3件が Bad の状態を作り、閾値を満たして negative_weight を上げる
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
        target_feedback = add_feedback(
            db_session, user_id, target, FeedbackAction.BAD, created_at=NOW
        )
        update_topic_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)
        before = _get_topic_preference(db_session, user_id, "llm")
        assert before is not None
        assert before.negative_weight > 0.0

        # Act — 閾値を満たすきっかけになった target の Bad を取り消す
        db_session.delete(target_feedback)
        db_session.flush()
        recompute_topic_preferences_after_removal(db_session, user_id, target.id, NOW)

        # Assert — 残り4件（Good2件・Bad2件）は閾値未満のため negative_weight が下がる
        after = _get_topic_preference(db_session, user_id, "llm")
        assert after is not None
        assert after.negative_weight == pytest.approx(0.0)
        assert after.effective_weight == pytest.approx(after.positive_weight)

    def test_resets_to_initial_state_once_all_feedback_is_removed(
        self, db_session: Session
    ) -> None:
        # Arrange — 3件とも Bad で閾値（3/5）を満たす状態を作る
        user_id = uuid.uuid4()
        articles_and_feedback = []
        for index in range(3):
            article = make_article(db_session, title=f"bad-{index}", topics=["llm"])
            feedback = add_feedback(
                db_session,
                user_id,
                article,
                FeedbackAction.BAD,
                created_at=NOW - timedelta(days=2 - index),
            )
            articles_and_feedback.append((article, feedback))
        last_article, _ = articles_and_feedback[-1]
        update_topic_preferences(db_session, user_id, last_article.id, FeedbackAction.BAD, NOW)
        before = _get_topic_preference(db_session, user_id, "llm")
        assert before is not None
        assert before.negative_weight > 0.0

        # Act — すべての Bad を取り消す
        for article, feedback in articles_and_feedback:
            db_session.delete(feedback)
            db_session.flush()
            recompute_topic_preferences_after_removal(db_session, user_id, article.id, NOW)

        # Assert — 行は残るが初期状態（全て 0）へ戻る（行の削除はしない設計）
        after = _get_topic_preference(db_session, user_id, "llm")
        assert after is not None
        assert after.positive_weight == pytest.approx(0.0)
        assert after.negative_weight == pytest.approx(0.0)
        assert after.effective_weight == pytest.approx(0.0)

    def test_leaves_positive_weight_unchanged(self, db_session: Session) -> None:
        """受入基準: Good を取り消した場合と同様、positive_weight は据え置く。"""
        # Arrange — Good で positive_weight を作り、別途 Bad で閾値を満たす
        user_id = uuid.uuid4()
        good_article = make_article(db_session, title="good", topics=["llm"])
        update_topic_preferences(db_session, user_id, good_article.id, FeedbackAction.GOOD, NOW)
        bad_1 = make_article(db_session, title="bad-1", topics=["llm"])
        add_feedback(
            db_session, user_id, bad_1, FeedbackAction.BAD, created_at=NOW - timedelta(days=2)
        )
        bad_2 = make_article(db_session, title="bad-2", topics=["llm"])
        add_feedback(
            db_session, user_id, bad_2, FeedbackAction.BAD, created_at=NOW - timedelta(days=1)
        )
        target = make_article(db_session, title="bad-3", topics=["llm"])
        target_feedback = add_feedback(
            db_session, user_id, target, FeedbackAction.BAD, created_at=NOW
        )
        update_topic_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)
        before = _get_topic_preference(db_session, user_id, "llm")
        assert before is not None
        positive_before = before.positive_weight
        assert positive_before > 0.0

        # Act
        db_session.delete(target_feedback)
        db_session.flush()
        recompute_topic_preferences_after_removal(db_session, user_id, target.id, NOW)

        # Assert — negative_weight は下がるが positive_weight は変わらない
        after = _get_topic_preference(db_session, user_id, "llm")
        assert after is not None
        assert after.positive_weight == pytest.approx(positive_before)
        assert after.negative_weight == pytest.approx(0.0)

    def test_does_nothing_when_the_article_has_no_topics(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=[])

        # Act / Assert — 例外にならない
        recompute_topic_preferences_after_removal(db_session, user_id, article.id, NOW)

    def test_does_nothing_when_the_article_does_not_exist(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()

        # Act / Assert — 例外にならない
        recompute_topic_preferences_after_removal(db_session, user_id, uuid.uuid4(), NOW)


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


class TestLoadWeightedInterestArticlesConfidence:
    """受入基準「`effective_interest` の計算に confidence が反映される」（Issue #20）。"""

    def test_an_unanalyzed_article_weighs_less_than_a_fully_analyzed_one(
        self, db_session: Session
    ) -> None:
        # Arrange — 同じ経路（手動登録）・同じ日時で、シグナルの充足度だけが違う 2 件
        user_id = uuid.uuid4()
        analyzed = make_article(
            db_session, title="解析済み", topics=["llm"], embedding=make_embedding(0)
        )
        add_user_article(db_session, user_id, analyzed, ArticleOrigin.MANUAL)
        unanalyzed = make_article(
            db_session,
            title="クリックされただけ",
            topics=[],
            embedding=None,
            analysis_status=JobStatus.PENDING.value,
        )
        add_user_article(db_session, user_id, unanalyzed, ArticleOrigin.MANUAL)

        # Act
        records = load_weighted_interest_articles(db_session, user_id, NOW)
        weight_by_topics = {record.topics: record.weight for record in records}

        # Assert
        assert weight_by_topics[("llm",)] > weight_by_topics[()]

    def test_keeps_a_positive_weight_even_without_any_signal(self, db_session: Session) -> None:
        # Arrange — 寄与をゼロにはしない（min_confidence の下限が効く）
        user_id = uuid.uuid4()
        article = make_article(
            db_session, topics=[], embedding=None, analysis_status=JobStatus.PENDING.value
        )
        add_user_article(db_session, user_id, article, ArticleOrigin.MANUAL)

        # Act
        records = load_weighted_interest_articles(db_session, user_id, NOW)

        # Assert
        assert len(records) == 1
        assert records[0].weight > 0.0

    def test_a_fully_analyzed_article_uses_the_maximum_confidence(
        self, db_session: Session
    ) -> None:
        # Arrange — 全シグナルが揃った記事は confidence による減衰を受けない
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["llm"], embedding=make_embedding(0))
        add_user_article(db_session, user_id, article, ArticleOrigin.MANUAL)
        config = get_scoring_config()

        # Act
        records = load_weighted_interest_articles(db_session, user_id, NOW)

        # Assert — explicit_weight（manual=1.0）× recency_decay（当日=1.0）そのもの
        assert records[0].weight == pytest.approx(config.feedback_weights.manual)


class TestCountInterestArticlesByOrigin:
    """`GET /api/interests/summary` の origin 別内訳（Issue #92）が使う集計関数。

    `load_weighted_interest_articles` と完全に同じ母集団（`user_articles` の
    5経路 + `article_feedback` の good のみ、user_articles 優先でマージ）を
    使わないと、画面の内訳が実際のプロファイル寄与と食い違う（母集団のずれは
    嘘の数字になる）ため、その一致を検証する。
    """

    def test_counts_each_origin_from_user_articles(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        manual_article = make_article(db_session, title="手動登録")
        add_user_article(db_session, user_id, manual_article, ArticleOrigin.MANUAL)
        saved_article = make_article(db_session, title="保存")
        add_user_article(db_session, user_id, saved_article, ArticleOrigin.SAVED)
        read_full_article = make_article(db_session, title="全文閲覧")
        add_user_article(db_session, user_id, read_full_article, ArticleOrigin.READ_FULL)
        clicked_article = make_article(db_session, title="クリック")
        add_user_article(db_session, user_id, clicked_article, ArticleOrigin.CLICKED)

        # Act
        counts = count_interest_articles_by_origin(db_session, user_id)

        # Assert
        assert counts[ArticleOrigin.MANUAL] == 1
        assert counts[ArticleOrigin.SAVED] == 1
        assert counts[ArticleOrigin.READ_FULL] == 1
        assert counts[ArticleOrigin.CLICKED] == 1
        assert counts[ArticleOrigin.GOOD] == 0

    def test_counts_an_article_feedback_only_good_as_good(self, db_session: Session) -> None:
        """受入基準: `user_articles` に無く `article_feedback` だけにある good は Good扱い。"""
        # Arrange — §7.1 手順1の反映前に読んだ場合の取りこぼしを補う経路
        # （`load_weighted_interest_articles` docstring と同じ想定）
        user_id = uuid.uuid4()
        article = make_article(db_session, title="フィードバックのみ")
        add_feedback(db_session, user_id, article, FeedbackAction.GOOD)

        # Act
        counts = count_interest_articles_by_origin(db_session, user_id)

        # Assert
        assert counts[ArticleOrigin.GOOD] == 1
        assert sum(counts.values()) == 1

    def test_user_articles_origin_takes_precedence_over_article_feedback_good(
        self, db_session: Session
    ) -> None:
        """受入基準: 同じ記事に両方の記録があれば `user_articles` 側の origin を数える。"""
        # Arrange — 保存した記事に後から Good も付けたケース（§7.1 手順1で
        # user_articles にも記録されるのが通常だが、ここでは意図的にズラして
        # user_articles 優先のマージを確かめる）
        user_id = uuid.uuid4()
        article = make_article(db_session, title="保存してからGood")
        add_user_article(db_session, user_id, article, ArticleOrigin.SAVED)
        add_feedback(db_session, user_id, article, FeedbackAction.GOOD)

        # Act
        counts = count_interest_articles_by_origin(db_session, user_id)

        # Assert
        assert counts[ArticleOrigin.SAVED] == 1
        assert counts[ArticleOrigin.GOOD] == 0

    def test_returns_zero_counts_for_a_user_without_interest_articles(
        self, db_session: Session
    ) -> None:
        # Arrange / Act
        counts = count_interest_articles_by_origin(db_session, uuid.uuid4())

        # Assert
        assert sum(counts.values()) == 0
        assert set(counts) == {
            ArticleOrigin.MANUAL,
            ArticleOrigin.GOOD,
            ArticleOrigin.SAVED,
            ArticleOrigin.READ_FULL,
            ArticleOrigin.CLICKED,
        }

    def test_does_not_mix_in_another_users_articles(self, db_session: Session) -> None:
        # Arrange
        other_article = make_article(db_session, title="他人の記事")
        add_user_article(db_session, uuid.uuid4(), other_article, ArticleOrigin.MANUAL)

        # Act
        counts = count_interest_articles_by_origin(db_session, uuid.uuid4())

        # Assert
        assert sum(counts.values()) == 0


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


class TestUpdateSourcePreferencesGoodAndSave:
    """受入基準「Good した記事の情報源に対する選好が増える」（Issue #34）。"""

    def test_good_increases_positive_and_effective_weight(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, source_domain="blog.example.jp")

        # Act
        update_source_preferences(db_session, user_id, article.id, FeedbackAction.GOOD, NOW)

        # Assert — 増分は config/scoring.yaml の feedback_weights.good
        config = get_scoring_config()
        preference = _get_source_preference(db_session, user_id, "blog.example.jp")
        assert preference is not None
        assert preference.positive_weight == pytest.approx(config.feedback_weights.good)
        assert preference.effective_weight == pytest.approx(config.feedback_weights.good)

    def test_save_increases_positive_weight_by_the_save_increment(
        self, db_session: Session
    ) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, source_domain="blog.example.jp")

        # Act
        update_source_preferences(db_session, user_id, article.id, FeedbackAction.SAVE, NOW)

        # Assert
        config = get_scoring_config()
        preference = _get_source_preference(db_session, user_id, "blog.example.jp")
        assert preference is not None
        assert preference.positive_weight == pytest.approx(config.feedback_weights.save)

    def test_repeated_good_accumulates_the_positive_weight(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        first = make_article(db_session, title="1本目", source_domain="blog.example.jp")
        update_source_preferences(db_session, user_id, first.id, FeedbackAction.GOOD, NOW)

        # Act — 同じ情報源の別記事へ Good を重ねる
        second = make_article(db_session, title="2本目", source_domain="blog.example.jp")
        update_source_preferences(db_session, user_id, second.id, FeedbackAction.GOOD, NOW)

        # Assert
        config = get_scoring_config()
        preference = _get_source_preference(db_session, user_id, "blog.example.jp")
        assert preference is not None
        assert preference.positive_weight == pytest.approx(config.feedback_weights.good * 2)

    def test_does_not_touch_another_source(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, source_domain="blog.example.jp")

        # Act
        update_source_preferences(db_session, user_id, article.id, FeedbackAction.GOOD, NOW)

        # Assert
        assert _get_source_preference(db_session, user_id, "other.example.jp") is None

    def test_does_nothing_when_the_article_does_not_exist(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()

        # Act / Assert — 例外にならない
        update_source_preferences(db_session, user_id, uuid.uuid4(), FeedbackAction.GOOD, NOW)


class TestUpdateSourcePreferencesBad:
    """受入基準「単発の Bad では下がらず、繰り返された場合にのみ下がる」（Issue #34）。"""

    def test_a_single_bad_does_not_lower_the_source_weight(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, source_domain="blog.example.jp")
        add_feedback(db_session, user_id, article, FeedbackAction.BAD)

        # Act
        update_source_preferences(db_session, user_id, article.id, FeedbackAction.BAD, NOW)

        # Assert — 閾値未達のため行すら作られない
        assert _get_source_preference(db_session, user_id, "blog.example.jp") is None

    def test_lowers_the_weight_once_three_of_five_recent_are_bad(self, db_session: Session) -> None:
        # Arrange — 同一情報源の記事5件を Good/Good/Bad/Bad まで積み、
        # 3件目の Bad を送って閾値（3/5）に達させる
        user_id = uuid.uuid4()
        domain = "blog.example.jp"
        for index, action in enumerate((FeedbackAction.GOOD, FeedbackAction.GOOD)):
            article = make_article(db_session, title=f"good-{index}", source_domain=domain)
            add_feedback(
                db_session, user_id, article, action, created_at=NOW - timedelta(days=4 - index)
            )
        for index in range(2):
            article = make_article(db_session, title=f"bad-{index}", source_domain=domain)
            add_feedback(
                db_session,
                user_id,
                article,
                FeedbackAction.BAD,
                created_at=NOW - timedelta(days=2 - index),
            )
        target = make_article(db_session, title="bad-3", source_domain=domain)
        add_feedback(db_session, user_id, target, FeedbackAction.BAD, created_at=NOW)

        # Act
        update_source_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)

        # Assert
        config = get_scoring_config()
        preference = _get_source_preference(db_session, user_id, domain)
        assert preference is not None
        assert preference.negative_weight == pytest.approx(config.source_preference.decay_step)
        assert preference.effective_weight == pytest.approx(-config.source_preference.decay_step)

    def test_counts_only_feedback_for_the_same_source(self, db_session: Session) -> None:
        # Arrange — 別ドメインの Bad は数えない
        user_id = uuid.uuid4()
        for index in range(2):
            other = make_article(
                db_session, title=f"other-{index}", source_domain="other.example.jp"
            )
            add_feedback(
                db_session,
                user_id,
                other,
                FeedbackAction.BAD,
                created_at=NOW - timedelta(days=2 - index),
            )
        target = make_article(db_session, title="target", source_domain="blog.example.jp")
        add_feedback(db_session, user_id, target, FeedbackAction.BAD, created_at=NOW)

        # Act
        update_source_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)

        # Assert — 対象ドメインの直近 Bad は 1 件だけのため下がらない
        assert _get_source_preference(db_session, user_id, "blog.example.jp") is None


class TestRecomputeSourcePreferencesAfterRemoval:
    """フィードバック取り消し後に情報源選好の抑制が残り続けないことを検証する。"""

    def test_clears_the_negative_weight_once_the_bad_feedback_is_removed(
        self, db_session: Session
    ) -> None:
        # Arrange — 閾値に達して negative_weight が付いた状態を作る
        user_id = uuid.uuid4()
        domain = "blog.example.jp"
        articles = []
        for index in range(3):
            article = make_article(db_session, title=f"bad-{index}", source_domain=domain)
            add_feedback(
                db_session,
                user_id,
                article,
                FeedbackAction.BAD,
                created_at=NOW - timedelta(days=2 - index),
            )
            articles.append(article)
        target = articles[-1]
        update_source_preferences(db_session, user_id, target.id, FeedbackAction.BAD, NOW)
        assert _get_source_preference(db_session, user_id, domain) is not None

        # Act — Bad を1件取り消して閾値を下回らせる
        db_session.execute(
            delete(ArticleFeedback).where(
                ArticleFeedback.user_id == user_id, ArticleFeedback.article_id == target.id
            )
        )
        db_session.flush()
        recompute_source_preferences_after_removal(db_session, user_id, target.id, NOW)

        # Assert
        preference = _get_source_preference(db_session, user_id, domain)
        assert preference is not None
        assert preference.negative_weight == pytest.approx(0.0)
        assert preference.effective_weight == pytest.approx(preference.positive_weight)

    def test_does_nothing_when_the_article_does_not_exist(self, db_session: Session) -> None:
        # Arrange
        user_id = uuid.uuid4()

        # Act / Assert — 例外にならない
        recompute_source_preferences_after_removal(db_session, user_id, uuid.uuid4(), NOW)
