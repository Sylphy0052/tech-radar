"""`embed_article` ジョブハンドラ（`PROJECT_SPEC.md` §6.2, Issue #12 T3, Issue #9 T15）。

記事へ Embedding を付与し、登録行があれば URL 登録の状態を `completed` にする
（登録の状態遷移における最終段階）。payload に `registration_id` が無い場合
（巡回由来のジョブ）は登録行の更新・失敗記録をすべて省略する
（`techradar.jobs.handlers.fetch_article` と同じ設計）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article, ArticleRegistration
from techradar.embedding import (
    EmbeddingProvider,
    QwenEmbeddingProvider,
    embed_articles,
    needs_embedding,
)
from techradar.jobs.handlers._shared import (
    load_registration,
    record_registration_failure_safely,
    run_job_in_thread,
    start_registration_step,
)
from techradar.jobs.handlers.errors import classify_embedding_error
from techradar.jobs.registry import JobContext, JobHandler


def process_embed_article(
    session: Session,
    context: JobContext,
    settings: Settings,
    provider: EmbeddingProvider,
) -> None:
    """`embed_article` ジョブ 1 件分の処理。"""
    registration_id = _optional_registration_id(context.payload)
    article_id = uuid.UUID(context.payload["article_id"])

    registration = _load_registration_if_present(session, registration_id)
    if registration_id is not None and registration is None:
        return

    if registration is not None:
        # embed_article の実行中 status は `analyzing` に集約される
        # （`techradar.jobs.status.running_status_for`）。embedding 専用の
        # 実行中 status は無いため、ここで設定される値も analyzing になる。
        start_registration_step(session, registration, JobType.EMBED_ARTICLE)

    article = session.get(Article, article_id)
    if article is None:
        # 記事行が削除済み。リトライしても解決しないため打ち切る。
        return

    # 分類済みの例外だけでなく想定外の例外も記録する。記録しないまま抜けると
    # 登録が実行中 status・理由なしのまま残り、UI からは永久に処理中に見える。
    try:
        if needs_embedding(article):
            embed_articles(session, provider, [article])

        if registration is not None:
            # URL 登録の状態遷移（`PROJECT_SPEC.md` §6.2）の終端。
            registration.status = JobStatus.COMPLETED.value
        session.flush()
    except Exception as exc:
        if registration_id is not None:
            record_registration_failure_safely(
                session,
                registration_id,
                classify_embedding_error(exc),
                context=context,
                settings=settings,
            )
        raise


def _optional_registration_id(payload: dict[str, Any]) -> uuid.UUID | None:
    """payload から `registration_id` を取り出す。無ければ巡回由来として `None`。"""
    raw = payload.get("registration_id")
    if raw is None:
        return None
    return uuid.UUID(raw)


def _load_registration_if_present(
    session: Session, registration_id: uuid.UUID | None
) -> ArticleRegistration | None:
    """`registration_id` があれば登録行を取得する。無ければ `None`（巡回由来）。"""
    if registration_id is None:
        return None
    return load_registration(session, registration_id)


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
