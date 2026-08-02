"""`rebuild_interest_clusters` ジョブハンドラを検証する結合テスト。

`PROJECT_SPEC.md` §8, Issue #15 段階 3。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import ArticleOrigin, JobType
from techradar.db.models import Article, UserArticle, UserInterestCluster
from techradar.jobs.handlers import rebuild_interest_clusters as rebuild_interest_clusters_module
from techradar.jobs.handlers.rebuild_interest_clusters import process_rebuild_interest_clusters
from techradar.jobs.registry import JobContext

EMBEDDING_DIM = 1024


def make_embedding(active_index: int = 0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[active_index] = 1.0
    return vector


def make_article(
    session: Session, *, topics: list[str] | None = None, embedding: list[float] | None = None
) -> Article:
    canonical_url = f"https://example.com/{uuid.uuid4().hex[:10]}"
    article = Article(
        canonical_url=canonical_url,
        original_url=canonical_url,
        title="タイトル",
        source_domain="example.com",
        topics=topics or [],
        embedding=embedding,
    )
    session.add(article)
    session.flush()
    return article


def add_user_article(session: Session, user_id: uuid.UUID, article: Article) -> None:
    session.add(
        UserArticle(
            user_id=user_id,
            article_id=article.id,
            origin=ArticleOrigin.GOOD.value,
            interest_weight=0.8,
        )
    )
    session.flush()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_context(payload: dict[str, object]) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.REBUILD_INTEREST_CLUSTERS,
        payload=payload,
        attempts=0,
    )


class TestProcessRebuildInterestClusters:
    def test_builds_clusters_for_the_users_interest_articles(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["AI"], embedding=make_embedding(0))
        add_user_article(db_session, user_id, article)
        context = make_context({"user_id": str(user_id)})

        # Act
        process_rebuild_interest_clusters(db_session, context, settings)

        # Assert
        clusters = db_session.scalars(
            select(UserInterestCluster).where(UserInterestCluster.user_id == user_id)
        ).all()
        assert len(clusters) >= 1

    def test_raises_for_a_malformed_user_id(self, db_session: Session, settings: Settings) -> None:
        """受入基準: payload の user_id は UUID として検証する。

        不正なら既存ハンドラと同じエラー型で失敗させる。
        """
        # Arrange
        context = make_context({"user_id": "not-a-uuid"})

        # Act / Assert
        with pytest.raises(ValueError):
            process_rebuild_interest_clusters(db_session, context, settings)

    def test_raises_for_a_missing_user_id(self, db_session: Session, settings: Settings) -> None:
        # Arrange
        context = make_context({})

        # Act / Assert
        with pytest.raises(KeyError):
            process_rebuild_interest_clusters(db_session, context, settings)

    def test_logs_the_cluster_count_at_info_level(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        info_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            rebuild_interest_clusters_module.logger,
            "info",
            lambda *args, **_kwargs: info_calls.append(args),
        )
        user_id = uuid.uuid4()
        article = make_article(db_session, topics=["AI"], embedding=make_embedding(0))
        add_user_article(db_session, user_id, article)
        context = make_context({"user_id": str(user_id)})

        # Act
        process_rebuild_interest_clusters(db_session, context, settings)

        # Assert
        assert len(info_calls) == 1
        message, *args = info_calls[0]
        assert (
            message == "rebuild_interest_clusters.completed job_id=%s user_id=%s cluster_count=%s"
        )
        assert args[0] == context.job_id
        assert args[1] == user_id
