"""全テーブルの CRUD と制約を検証する結合テスト。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from techradar.db import (
    EMBEDDING_DIMENSIONS,
    Article,
    ArticleFeedback,
    ArticleRegistration,
    Job,
    OperationLog,
    Recommendation,
    RecommendationRun,
    SourceRegistry,
    UserArticle,
    UserInterestCluster,
    UserTopicPreference,
)
from techradar.db.enums import (
    ArticleOrigin,
    BadReason,
    ContentType,
    FeedbackAction,
    JobStatus,
    JobType,
    RecommendationMode,
    SourceType,
)
from techradar.jobs.handlers.purge_recommendation_runs import build_expired_runs_delete
from techradar.recommendation.service import build_latest_run_select

COMPOSITE_INDEX_NAME = "ix_recommendation_runs_user_id_mode_generated_at"
GENERATED_AT_INDEX_NAME = "ix_recommendation_runs_generated_at"


def make_article(**overrides) -> Article:
    """テスト用の記事を組み立てる。"""
    suffix = uuid.uuid4().hex[:8]
    defaults = {
        "canonical_url": f"https://example.com/article-{suffix}",
        "original_url": f"https://example.com/article-{suffix}?utm_source=x",
        "title": "Example Article",
        "source_domain": "example.com",
        "language": "en",
        "published_at": datetime.now(UTC) - timedelta(days=1),
        "topics": ["MCP", "Context Engineering"],
        "technologies": ["Claude Code"],
        "source_authority": 0.9,
        "technical_quality": 0.8,
        "is_primary_source": True,
    }
    return Article(**{**defaults, **overrides})


class TestArticle:
    def test_persists_and_reads_back_all_analysis_fields(self, db_session: Session):
        # Arrange
        article = make_article(
            translated_title="日本語タイトル",
            summary_ja="日本語要約",
            body="本文",
            content_type=ContentType.IMPLEMENTATION,
            source_type=SourceType.OFFICIAL_BLOG,
        )

        # Act
        db_session.add(article)
        db_session.flush()
        stored = db_session.get(Article, article.id)

        # Assert
        assert stored is not None
        assert stored.translated_title == "日本語タイトル"
        assert stored.summary_ja == "日本語要約"
        assert stored.body == "本文"
        assert stored.topics == ["MCP", "Context Engineering"]
        assert stored.is_primary_source is True
        assert stored.is_dead is False
        assert stored.fetched_at is not None

    def test_rejects_duplicate_canonical_url(self, db_session: Session):
        # Arrange
        url = f"https://example.com/dup-{uuid.uuid4().hex[:8]}"
        db_session.add(make_article(canonical_url=url))
        db_session.flush()

        # Act / Assert — 重複排除の前提となる一意制約
        db_session.add(make_article(canonical_url=url))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_stores_embedding_with_configured_dimensions(self, db_session: Session):
        # Arrange
        vector = [0.1] * EMBEDDING_DIMENSIONS
        article = make_article(embedding=vector)

        # Act
        db_session.add(article)
        db_session.flush()
        db_session.expire(article)

        # Assert
        assert article.embedding is not None
        assert len(article.embedding) == EMBEDDING_DIMENSIONS

    def test_rejects_embedding_with_wrong_dimensions(self, db_session: Session):
        # Arrange — 別モデルへ差し替えた際の取り違えを DB 側でも検出する
        article = make_article(embedding=[0.1] * 512)

        # Act / Assert
        db_session.add(article)
        with pytest.raises(DBAPIError, match="expected 1024 dimensions"):
            db_session.flush()

    def test_marks_dead_link_without_deleting_the_row(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()

        # Act — リンク切れはソフト削除する
        article.is_dead = True
        db_session.flush()

        # Assert
        assert db_session.get(Article, article.id) is not None
        assert article.is_dead is True

    def test_defaults_duplicate_penalty_to_zero(self, db_session: Session):
        # Arrange
        article = make_article()

        # Act — 代表記事（重複なし）は duplicate_of_article_id を持たない
        db_session.add(article)
        db_session.flush()
        db_session.expire(article)

        # Assert
        assert article.duplicate_penalty == pytest.approx(0.0)
        assert article.duplicate_of_article_id is None

    def test_persists_duplicate_of_article_reference(self, db_session: Session):
        # Arrange — クラスタは同じ代表記事の id でグループ化する
        representative = make_article()
        db_session.add(representative)
        db_session.flush()
        duplicate = make_article(duplicate_of_article_id=representative.id, duplicate_penalty=0.5)

        # Act
        db_session.add(duplicate)
        db_session.flush()
        db_session.expire(duplicate)

        # Assert
        assert duplicate.duplicate_of_article_id == representative.id
        assert duplicate.duplicate_penalty == pytest.approx(0.5)

    def test_clears_duplicate_reference_when_representative_is_deleted(self, db_session: Session):
        # Arrange
        representative = make_article()
        db_session.add(representative)
        db_session.flush()
        duplicate = make_article(duplicate_of_article_id=representative.id)
        db_session.add(duplicate)
        db_session.flush()

        # Act — 代表記事が削除されても重複記事自体は残り、参照だけ外れる
        db_session.delete(representative)
        db_session.flush()
        db_session.expire(duplicate)

        # Assert
        assert db_session.get(Article, duplicate.id) is not None
        assert duplicate.duplicate_of_article_id is None


class TestUserArticle:
    def test_records_origin_and_weight(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()

        # Act
        entry = UserArticle(
            user_id=uuid.uuid4(),
            article_id=article.id,
            origin=ArticleOrigin.MANUAL,
            interest_weight=1.0,
        )
        db_session.add(entry)
        db_session.flush()

        # Assert
        assert entry.origin == ArticleOrigin.MANUAL
        assert entry.interest_weight == pytest.approx(1.0)
        assert entry.created_at is not None

    def test_rejects_same_article_twice_for_one_user(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()
        user_id = uuid.uuid4()
        db_session.add(
            UserArticle(
                user_id=user_id,
                article_id=article.id,
                origin=ArticleOrigin.MANUAL,
                interest_weight=1.0,
            )
        )
        db_session.flush()

        # Act / Assert
        db_session.add(
            UserArticle(
                user_id=user_id,
                article_id=article.id,
                origin=ArticleOrigin.GOOD,
                interest_weight=0.8,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_allows_different_users_to_hold_the_same_article(self, db_session: Session):
        # Arrange — 将来のマルチユーザー化を妨げないことの確認
        article = make_article()
        db_session.add(article)
        db_session.flush()

        # Act
        for _ in range(2):
            db_session.add(
                UserArticle(
                    user_id=uuid.uuid4(),
                    article_id=article.id,
                    origin=ArticleOrigin.GOOD,
                    interest_weight=0.8,
                )
            )
        db_session.flush()

        # Assert
        rows = db_session.scalars(
            select(UserArticle).where(UserArticle.article_id == article.id)
        ).all()
        assert len(rows) == 2


class TestArticleRegistration:
    def test_defaults_status_to_pending(self, db_session: Session):
        # Arrange / Act
        registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/article?utm_source=x",
            normalized_url="https://example.com/article",
        )
        db_session.add(registration)
        db_session.flush()
        db_session.expire(registration)

        # Assert
        assert registration.status == JobStatus.PENDING
        assert registration.article_id is None
        assert registration.job_id is None
        assert registration.error_reason is None
        assert registration.created_at is not None
        assert registration.updated_at is not None

    def test_rejects_duplicate_normalized_url_for_the_same_user(self, db_session: Session):
        # Arrange
        user_id = uuid.uuid4()
        db_session.add(
            ArticleRegistration(
                user_id=user_id,
                url="https://example.com/article",
                normalized_url="https://example.com/article",
            )
        )
        db_session.flush()

        # Act / Assert — 同じ URL の再登録で fetch ジョブを積み増さないための一意制約
        db_session.add(
            ArticleRegistration(
                user_id=user_id,
                url="https://example.com/article?utm_source=y",
                normalized_url="https://example.com/article",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_allows_different_users_to_register_the_same_normalized_url(self, db_session: Session):
        # Arrange — 将来のマルチユーザー化を妨げないことの確認
        # Act
        for _ in range(2):
            db_session.add(
                ArticleRegistration(
                    user_id=uuid.uuid4(),
                    url="https://example.com/article",
                    normalized_url="https://example.com/article",
                )
            )
        db_session.flush()

        # Assert
        rows = db_session.scalars(
            select(ArticleRegistration).where(
                ArticleRegistration.normalized_url == "https://example.com/article"
            )
        ).all()
        assert len(rows) == 2

    def test_clears_article_reference_when_the_article_is_deleted(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()
        registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/article",
            normalized_url="https://example.com/article",
            article_id=article.id,
            status=JobStatus.COMPLETED,
        )
        db_session.add(registration)
        db_session.flush()

        # Act — 取得済み記事が別の理由で削除されても、登録履歴自体は残す
        db_session.delete(article)
        db_session.flush()
        db_session.expire(registration)

        # Assert
        assert registration.article_id is None

    def test_clears_job_reference_when_the_job_is_deleted(self, db_session: Session):
        # Arrange
        job = Job(type=JobType.FETCH_ARTICLE, payload={})
        db_session.add(job)
        db_session.flush()
        registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/article",
            normalized_url="https://example.com/article",
            job_id=job.id,
        )
        db_session.add(registration)
        db_session.flush()

        # Act
        db_session.delete(job)
        db_session.flush()
        db_session.expire(registration)

        # Assert
        assert registration.job_id is None

    def test_stores_a_classified_error_reason_without_the_raw_exception_message(
        self, db_session: Session
    ):
        # Arrange / Act — 例外メッセージそのものではなく分類済みの理由のみを保持する
        registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/article",
            normalized_url="https://example.com/article",
            status=JobStatus.FAILED,
            error_reason="fetch_timeout",
        )
        db_session.add(registration)
        db_session.flush()
        db_session.expire(registration)

        # Assert
        assert registration.error_reason == "fetch_timeout"

    def test_advances_updated_at_when_the_status_changes(self, db_session: Session):
        # Arrange
        registration = ArticleRegistration(
            user_id=uuid.uuid4(),
            url="https://example.com/article",
            normalized_url="https://example.com/article",
        )
        db_session.add(registration)
        db_session.flush()
        db_session.expire(registration)
        created_at = registration.created_at
        first_updated_at = registration.updated_at

        # Act
        registration.status = JobStatus.FETCHING
        db_session.flush()
        db_session.expire(registration)

        # Assert — 登録時は created_at と同時刻、状態が進んだら後ろへ動く
        assert first_updated_at == created_at
        assert registration.updated_at > first_updated_at


class TestArticleFeedback:
    def test_stores_bad_with_optional_reason(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()

        # Act
        feedback = ArticleFeedback(
            user_id=uuid.uuid4(),
            article_id=article.id,
            action=FeedbackAction.BAD,
            reason=BadReason.TOO_SHALLOW,
        )
        db_session.add(feedback)
        db_session.flush()

        # Assert
        assert feedback.reason == BadReason.TOO_SHALLOW

    def test_allows_bad_without_reason(self, db_session: Session):
        # Arrange — Bad 理由は任意
        article = make_article()
        db_session.add(article)
        db_session.flush()

        # Act
        feedback = ArticleFeedback(
            user_id=uuid.uuid4(), article_id=article.id, action=FeedbackAction.BAD
        )
        db_session.add(feedback)
        db_session.flush()

        # Assert
        assert feedback.reason is None

    def test_keeps_feedback_separate_per_user(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()

        # Act
        db_session.add(
            ArticleFeedback(user_id=uuid.uuid4(), article_id=article.id, action=FeedbackAction.GOOD)
        )
        db_session.add(
            ArticleFeedback(user_id=uuid.uuid4(), article_id=article.id, action=FeedbackAction.BAD)
        )
        db_session.flush()

        # Assert
        rows = db_session.scalars(
            select(ArticleFeedback).where(ArticleFeedback.article_id == article.id)
        ).all()
        assert len(rows) == 2


class TestRecommendations:
    def test_stores_score_breakdown_in_reasons(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()
        run = RecommendationRun(
            user_id=uuid.uuid4(),
            source_article_id=article.id,
            mode=RecommendationMode.ARTICLE_BASED,
        )
        db_session.add(run)
        db_session.flush()

        # Act — スコア内訳を確認できること (PROJECT_SPEC.md §26-15)
        breakdown = {
            "interest_similarity": 0.72,
            "source_authority": 0.90,
            "freshness": 0.85,
            "bad_penalty": 0.0,
        }
        db_session.add(
            Recommendation(
                run_id=run.id,
                article_id=article.id,
                score=0.81,
                reasons=breakdown,
                rank=1,
            )
        )
        db_session.flush()

        # Assert
        stored = db_session.scalars(
            select(Recommendation).where(Recommendation.run_id == run.id)
        ).one()
        assert stored.reasons == breakdown
        assert stored.rank == 1

    def test_deletes_recommendations_when_run_is_removed(self, db_session: Session):
        # Arrange
        article = make_article()
        db_session.add(article)
        db_session.flush()
        run = RecommendationRun(user_id=uuid.uuid4(), mode=RecommendationMode.DISCOVER)
        db_session.add(run)
        db_session.flush()
        db_session.add(
            Recommendation(run_id=run.id, article_id=article.id, score=0.5, reasons={}, rank=1)
        )
        db_session.flush()

        # Act
        db_session.delete(run)
        db_session.flush()

        # Assert
        assert db_session.scalars(select(Recommendation)).all() == []


class TestSourceRegistry:
    def test_persists_authority_and_verified_flag(self, db_session: Session):
        # Arrange / Act
        entry = SourceRegistry(
            entity_name="OpenAI",
            domain="platform.openai.com",
            path_pattern="/docs",
            github_org="openai",
            source_type=SourceType.OFFICIAL_DOCUMENTATION,
            authority_score=1.0,
        )
        db_session.add(entry)
        db_session.flush()

        # Assert
        assert entry.authority_score == pytest.approx(1.0)
        assert entry.verified is False

    def test_rejects_duplicate_domain_when_path_pattern_is_null(self, db_session: Session):
        # Arrange — PostgreSQL は既定で NULL 同士を別の値として扱うため、
        # path_pattern を持たないドメインが何度でも登録できてしまわないことを確認する
        db_session.add(
            SourceRegistry(
                entity_name="Anthropic",
                domain="anthropic.com",
                path_pattern=None,
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.9,
            )
        )
        db_session.flush()

        # Act / Assert
        db_session.add(
            SourceRegistry(
                entity_name="Anthropic (duplicate)",
                domain="anthropic.com",
                path_pattern=None,
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.2,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_rejects_duplicate_domain_and_path_pattern(self, db_session: Session):
        # Arrange
        db_session.add(
            SourceRegistry(
                entity_name="OpenAI",
                domain="openai.com",
                path_pattern="/index",
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.9,
            )
        )
        db_session.flush()

        # Act / Assert
        db_session.add(
            SourceRegistry(
                entity_name="OpenAI (duplicate)",
                domain="openai.com",
                path_pattern="/index",
                source_type=SourceType.OFFICIAL_BLOG,
                authority_score=0.5,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestInterestProfile:
    def test_stores_multiple_clusters_per_user(self, db_session: Session):
        # Arrange — 関心は単一の平均ではなく複数クラスタで保持する
        user_id = uuid.uuid4()

        # Act
        for label, weight in [("AI Agent Engineering", 0.55), ("3D Point Cloud", 0.45)]:
            db_session.add(
                UserInterestCluster(
                    user_id=user_id,
                    label=label,
                    weight=weight,
                    topics=["MCP"],
                    centroid_embedding=[0.01] * EMBEDDING_DIMENSIONS,
                )
            )
        db_session.flush()

        # Assert
        clusters = db_session.scalars(
            select(UserInterestCluster).where(UserInterestCluster.user_id == user_id)
        ).all()
        assert len(clusters) == 2
        assert sum(c.weight for c in clusters) == pytest.approx(1.0)

    def test_keeps_positive_and_negative_weights_separately(self, db_session: Session):
        # Arrange — Bad は Good の単純な負数として扱わない
        preference = UserTopicPreference(
            user_id=uuid.uuid4(),
            topic="MCP",
            positive_weight=1.6,
            negative_weight=0.8,
            effective_weight=0.4,
        )

        # Act
        db_session.add(preference)
        db_session.flush()

        # Assert
        assert preference.positive_weight == pytest.approx(1.6)
        assert preference.negative_weight == pytest.approx(0.8)
        assert preference.effective_weight == pytest.approx(0.4)


class TestJobAndLog:
    def test_creates_job_with_pending_status_by_default(self, db_session: Session):
        # Arrange / Act
        job = Job(type=JobType.CRAWL_SOURCES, payload={"trigger": "manual"})
        db_session.add(job)
        db_session.flush()
        db_session.expire(job)

        # Assert
        assert job.status == JobStatus.PENDING
        assert job.attempts == 0
        assert job.payload == {"trigger": "manual"}

    def test_defaults_available_at_to_now(self, db_session: Session):
        # Arrange / Act
        job = Job(type=JobType.CRAWL_SOURCES, payload={})
        db_session.add(job)
        db_session.flush()
        db_session.expire(job)

        # Assert — 既定では即時実行可能。リトライ時のみ将来時刻へ後ろ倒しする。
        assert job.available_at is not None
        assert job.available_at <= datetime.now(UTC)

    def test_defers_retry_by_moving_available_at_forward(self, db_session: Session):
        # Arrange
        job = Job(type=JobType.FETCH_ARTICLE, payload={"url": "https://example.com"})
        db_session.add(job)
        db_session.flush()
        deferred_until = datetime.now(UTC) + timedelta(seconds=30)

        # Act
        job.available_at = deferred_until
        db_session.flush()

        # Assert — マップ漏れを検出するため、DB に保存された値を直接読む。
        stored = db_session.execute(
            text("SELECT available_at FROM jobs WHERE id = :id"), {"id": job.id}
        ).scalar_one()
        assert stored == deferred_until

    def test_records_failure_reason_and_attempts(self, db_session: Session):
        # Arrange
        job = Job(type=JobType.FETCH_ARTICLE, payload={"url": "https://example.com"})
        db_session.add(job)
        db_session.flush()

        # Act
        job.status = JobStatus.FAILED
        job.attempts = 3
        job.last_error = "timeout"
        db_session.flush()

        # Assert
        assert job.attempts == 3
        assert job.last_error == "timeout"

    def test_records_llm_usage_in_operation_log(self, db_session: Session):
        # Arrange / Act — 使用モデル・トークン数・処理時間を残す (§24 可観測性)
        log = OperationLog(
            operation="analyze_article",
            status="completed",
            model="claude-cli",
            input_tokens=1200,
            output_tokens=300,
            duration_ms=4500,
            details={"language": "en"},
        )
        db_session.add(log)
        db_session.flush()

        # Assert
        assert log.model == "claude-cli"
        assert log.input_tokens == 1200
        assert log.duration_ms == 4500
        assert log.created_at is not None

    def test_keeps_log_when_referenced_job_is_deleted(self, db_session: Session):
        # Arrange
        job = Job(type=JobType.EMBED_ARTICLE, payload={})
        db_session.add(job)
        db_session.flush()
        log = OperationLog(operation="embed_article", status="failed", job_id=job.id)
        db_session.add(log)
        db_session.flush()

        # Act
        db_session.delete(job)
        db_session.flush()
        db_session.expire(log)

        # Assert — ログは残り、参照だけが外れる
        assert db_session.get(OperationLog, log.id) is not None
        assert log.job_id is None


class TestArticleDeletionCascade:
    def test_removes_dependent_rows_and_clears_optional_references(self, db_session: Session):
        # Arrange — 記事に紐づく全種類の行を作る
        article = make_article()
        db_session.add(article)
        db_session.flush()
        user_id = uuid.uuid4()
        db_session.add(
            UserArticle(
                user_id=user_id,
                article_id=article.id,
                origin=ArticleOrigin.MANUAL,
                interest_weight=1.0,
            )
        )
        db_session.add(
            ArticleFeedback(user_id=user_id, article_id=article.id, action=FeedbackAction.GOOD)
        )
        run = RecommendationRun(
            user_id=user_id,
            source_article_id=article.id,
            mode=RecommendationMode.ARTICLE_BASED,
        )
        db_session.add(run)
        db_session.flush()
        db_session.add(
            Recommendation(run_id=run.id, article_id=article.id, score=0.5, reasons={}, rank=1)
        )
        log = OperationLog(operation="analyze_article", status="completed", article_id=article.id)
        db_session.add(log)
        db_session.flush()

        # Act
        db_session.delete(article)
        db_session.flush()
        db_session.expire_all()

        # Assert — 従属データは消え、任意参照は NULL になり行自体は残る
        assert db_session.scalars(select(UserArticle)).all() == []
        assert db_session.scalars(select(ArticleFeedback)).all() == []
        assert db_session.scalars(select(Recommendation)).all() == []
        assert db_session.get(RecommendationRun, run.id) is not None
        assert run.source_article_id is None
        assert db_session.get(OperationLog, log.id) is not None
        assert log.article_id is None


class TestUpdates:
    def test_updates_topic_preference_weights(self, db_session: Session):
        # Arrange — Good/Bad の蓄積で更新される中核テーブル
        preference = UserTopicPreference(
            user_id=uuid.uuid4(), topic="MCP", positive_weight=0.8, effective_weight=0.8
        )
        db_session.add(preference)
        db_session.flush()

        # Act
        preference.negative_weight = 0.8
        preference.effective_weight = 0.0
        db_session.flush()
        db_session.expire(preference)

        # Assert
        assert preference.negative_weight == pytest.approx(0.8)
        assert preference.effective_weight == pytest.approx(0.0)

    def test_updates_source_authority_for_misclassified_source(self, db_session: Session):
        # Arrange — 誤った公式判定を修正できること (PROJECT_SPEC.md §27 運用)
        entry = SourceRegistry(
            entity_name="Example",
            domain="misjudged.example.com",
            path_pattern="/blog",
            source_type=SourceType.OFFICIAL_BLOG,
            authority_score=0.9,
        )
        db_session.add(entry)
        db_session.flush()

        # Act
        entry.authority_score = 0.45
        entry.source_type = SourceType.TECH_MEDIA
        entry.verified = True
        db_session.flush()
        db_session.expire(entry)

        # Assert
        assert entry.authority_score == pytest.approx(0.45)
        assert entry.source_type == SourceType.TECH_MEDIA
        assert entry.verified is True

    def test_deletes_interest_cluster(self, db_session: Session):
        # Arrange
        cluster = UserInterestCluster(
            user_id=uuid.uuid4(),
            label="AI Agent Engineering",
            weight=1.0,
            topics=["MCP"],
        )
        db_session.add(cluster)
        db_session.flush()
        cluster_id = cluster.id

        # Act
        db_session.delete(cluster)
        db_session.flush()

        # Assert
        assert db_session.get(UserInterestCluster, cluster_id) is None


class TestSchema:
    def test_creates_hnsw_index_for_cosine_similarity(self, migrated_engine: Engine):
        # Arrange / Act
        with migrated_engine.connect() as connection:
            indexdef = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_articles_embedding_hnsw'"
                )
            ).scalar_one()

        # Assert
        assert "USING hnsw" in indexdef
        assert "vector_cosine_ops" in indexdef

    def test_enables_pgvector_extension(self, migrated_engine: Engine):
        # Arrange / Act
        with migrated_engine.connect() as connection:
            extension = connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()

        # Assert
        assert extension == "vector"


def find_indexdef(engine: Engine, indexname: str) -> str | None:
    """`pg_indexes` からインデックス定義を引く（存在しなければ None）。"""
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": indexname},
        ).scalar_one_or_none()


def explain(session: Session, statement) -> str:
    """SELECT/DELETE 文の実行計画を文字列で返す。

    テスト DB は行数が少なくプランナが Seq Scan を選ぶため、`enable_seqscan`
    を切ってインデックスが使える形になっているかどうかだけを見る。
    """
    session.execute(text("SET LOCAL enable_seqscan = off"))
    compiled = statement.compile(dialect=session.get_bind().dialect)
    rows = session.connection().exec_driver_sql(f"EXPLAIN {compiled}", compiled.params).fetchall()
    return "\n".join(row[0] for row in rows)


class TestRecommendationRunIndexes:
    """Issue #32: 保持期間削除と直近 run 取得がインデックスを使うことを検証する。"""

    def test_defines_composite_index_for_latest_run_lookup(self, migrated_engine: Engine):
        # Arrange / Act
        indexdef = find_indexdef(migrated_engine, COMPOSITE_INDEX_NAME)

        # Assert
        assert indexdef is not None
        assert "(user_id, mode, generated_at DESC, id DESC)" in indexdef

    def test_defines_generated_at_index_for_retention_purge(self, migrated_engine: Engine):
        # Arrange / Act
        indexdef = find_indexdef(migrated_engine, GENERATED_AT_INDEX_NAME)

        # Assert
        assert indexdef is not None
        assert "(generated_at)" in indexdef

    def test_drops_redundant_user_id_only_index(self, migrated_engine: Engine):
        """複合インデックスの前方一致で代替できる単独インデックスは残さない。"""
        # Arrange / Act
        indexdef = find_indexdef(migrated_engine, "ix_recommendation_runs_user_id")

        # Assert
        assert indexdef is None

    def test_retention_delete_uses_generated_at_index(self, db_session: Session):
        # Arrange
        cutoff = datetime.now(UTC) - timedelta(days=30)
        statement = build_expired_runs_delete(cutoff)

        # Act
        plan = explain(db_session, statement)

        # Assert
        assert GENERATED_AT_INDEX_NAME in plan

    def test_latest_run_lookup_uses_composite_index(self, db_session: Session):
        # Arrange
        statement = build_latest_run_select(uuid.uuid4(), RecommendationMode.DISCOVER)

        # Act
        plan = explain(db_session, statement)

        # Assert
        assert COMPOSITE_INDEX_NAME in plan
