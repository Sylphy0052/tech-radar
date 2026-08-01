"""`embed_article` ジョブハンドラ（`PROJECT_SPEC.md` §6.2, Issue #12 T3）。

記事へ Embedding を付与し、URL 登録の状態を `completed` にする
（登録の状態遷移における最終段階）。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article
from techradar.embedding import (
    EmbeddingProvider,
    QwenEmbeddingProvider,
    embed_articles,
    needs_embedding,
)
from techradar.embedding.errors import EmbeddingError
from techradar.jobs.handlers._shared import (
    load_registration,
    record_registration_failure,
    run_job_in_thread,
)
from techradar.jobs.handlers.errors import classify_embedding_error
from techradar.jobs.registry import JobContext, JobHandler
from techradar.jobs.status import running_status_for


def process_embed_article(
    session: Session,
    context: JobContext,
    settings: Settings,
    provider: EmbeddingProvider,
) -> None:
    """`embed_article` ジョブ 1 件分の処理。"""
    registration_id = uuid.UUID(context.payload["registration_id"])
    article_id = uuid.UUID(context.payload["article_id"])

    registration = load_registration(session, registration_id)
    if registration is None:
        return

    # embed_article の実行中 status は `analyzing` に集約される
    # （`techradar.jobs.status.running_status_for`）。embedding 専用の
    # 実行中 status は無いため、ここで得られる値も analyzing になる。
    registration.status = running_status_for(JobType.EMBED_ARTICLE).value
    session.flush()

    article = session.get(Article, article_id)
    if article is None:
        # 記事行が削除済み。リトライしても解決しないため打ち切る。
        return

    if needs_embedding(article):
        try:
            embed_articles(session, provider, [article])
        except EmbeddingError as exc:
            reason = classify_embedding_error(exc)
            record_registration_failure(
                session, registration, reason, context=context, settings=settings
            )
            raise

    # URL 登録の状態遷移（`PROJECT_SPEC.md` §6.2）の終端。
    registration.status = JobStatus.COMPLETED.value
    session.flush()


def make_embed_article_handler(
    settings: Settings | None = None, provider: EmbeddingProvider | None = None
) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `embed_article` ハンドラを作る。"""
    resolved_settings = settings or get_settings()
    resolved_provider = provider or QwenEmbeddingProvider(resolved_settings)

    async def _handle(context: JobContext) -> None:
        def _operation(session: Session, ctx: JobContext, config: Settings) -> None:
            process_embed_article(session, ctx, config, resolved_provider)

        await run_job_in_thread(context, resolved_settings, _operation)

    return _handle
