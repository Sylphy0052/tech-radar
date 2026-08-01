"""`analyze_article` ジョブハンドラ（`PROJECT_SPEC.md` §6.2, Issue #12 T3）。

記事を解析して構造化データを保存し、`embed_article` を積む。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from techradar.analysis import analyze_article as run_analysis
from techradar.analysis import needs_analysis
from techradar.config import Settings, get_settings
from techradar.db.enums import JobType
from techradar.db.models import Article
from techradar.jobs.handlers._shared import (
    load_registration,
    record_registration_failure,
    run_job_in_thread,
)
from techradar.jobs.handlers.errors import classify_analysis_error
from techradar.jobs.queue import enqueue
from techradar.jobs.registry import JobContext, JobHandler
from techradar.jobs.status import running_status_for
from techradar.llm import ClaudeCliProvider, LLMProvider
from techradar.llm.errors import LLMError


def process_analyze_article(
    session: Session,
    context: JobContext,
    settings: Settings,
    provider: LLMProvider,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """`analyze_article` ジョブ 1 件分の処理。"""
    registration_id = uuid.UUID(context.payload["registration_id"])
    article_id = uuid.UUID(context.payload["article_id"])

    registration = load_registration(session, registration_id)
    if registration is None:
        return

    registration.status = running_status_for(JobType.ANALYZE_ARTICLE).value
    session.flush()

    article = session.get(Article, article_id)
    if article is None:
        # 記事行が削除済み。リトライしても解決しないため打ち切る。
        return

    if needs_analysis(article):
        try:
            run_analysis(session, provider, article, job_id=context.job_id, sleep=sleep)
        except LLMError as exc:
            reason = classify_analysis_error(exc)
            record_registration_failure(
                session, registration, reason, context=context, settings=settings
            )
            raise

    next_job = enqueue(
        session,
        JobType.EMBED_ARTICLE,
        {"registration_id": str(registration.id), "article_id": str(article.id)},
    )
    registration.job_id = next_job.id
    session.flush()


def make_analyze_article_handler(
    settings: Settings | None = None, provider: LLMProvider | None = None
) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `analyze_article` ハンドラを作る。"""
    resolved_settings = settings or get_settings()
    resolved_provider = provider or _default_provider(resolved_settings)

    async def _handle(context: JobContext) -> None:
        def _operation(session: Session, ctx: JobContext, config: Settings) -> None:
            process_analyze_article(session, ctx, config, resolved_provider)

        await run_job_in_thread(context, resolved_settings, _operation)

    return _handle


def _default_provider(settings: Settings) -> LLMProvider:
    """本番用の既定プロバイダーを作る。"""

    return ClaudeCliProvider(settings)
