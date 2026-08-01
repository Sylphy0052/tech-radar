"""`fetch_article` ジョブハンドラ（`PROJECT_SPEC.md` §6.2, Issue #12 T3）。

URL から記事を取得・保存し、手動登録の関心記事として `user_articles` へ
追加したうえで `analyze_article` を積む。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db.enums import ArticleOrigin, JobType
from techradar.db.models import UserArticle
from techradar.fetcher.errors import FetchError
from techradar.fetcher.service import ingest_article
from techradar.jobs.handlers._shared import (
    load_registration,
    record_registration_failure,
    run_job_in_thread,
)
from techradar.jobs.handlers.errors import classify_fetch_error
from techradar.jobs.queue import enqueue
from techradar.jobs.registry import JobContext, JobHandler
from techradar.jobs.status import running_status_for

# 手動 URL 登録の関心重み（`PROJECT_SPEC.md` §7.1）。
MANUAL_REGISTRATION_INTEREST_WEIGHT = 1.0


def process_fetch_article(session: Session, context: JobContext, settings: Settings) -> None:
    """`fetch_article` ジョブ 1 件分の処理。"""
    registration_id = uuid.UUID(context.payload["registration_id"])
    url = context.payload["url"]

    registration = load_registration(session, registration_id)
    if registration is None:
        return

    registration.status = running_status_for(JobType.FETCH_ARTICLE).value
    session.flush()

    try:
        result = ingest_article(session, url, settings=settings)
    except FetchError as exc:
        reason = classify_fetch_error(exc)
        record_registration_failure(
            session, registration, reason, context=context, settings=settings
        )
        raise

    _link_user_article(session, registration.user_id, result.article.id)

    next_job = enqueue(
        session,
        JobType.ANALYZE_ARTICLE,
        {"registration_id": str(registration.id), "article_id": str(result.article.id)},
    )
    registration.article_id = result.article.id
    registration.job_id = next_job.id
    # fetch 完了時点では status を `analyzing` にしない。各段階の責務を分け、
    # 「今どの段階が実際に動いているか」を、その段階のハンドラ自身が開始時に
    # 反映する設計にする。ここで先回りして analyzing にすると、
    # analyze_article ジョブがまだ claim されていない間も analyzing と表示され、
    # 表示と実処理の段階が食い違う。
    session.flush()


def _link_user_article(session: Session, user_id: uuid.UUID, article_id: uuid.UUID) -> None:
    """記事をユーザーの関心記事へ登録する。

    既に同じ (user_id, article_id) の行があれば何もしない。同じ記事へ
    複数の登録（別 URL が同じ記事に解決した等）が辿り着くことがあり、
    `uq_user_articles_user_id_article_id` があるため二重登録できない。
    事前確認だけでは、複数ワーカーが同時に別の登録から同じ記事へ辿り着く
    競合を完全には防げないため、一意制約違反は SAVEPOINT の中で吸収する
    （`techradar.llm.retry._record` と同じ方針）。
    """
    existing = session.scalar(
        select(UserArticle).where(
            UserArticle.user_id == user_id, UserArticle.article_id == article_id
        )
    )
    if existing is not None:
        return

    try:
        with session.begin_nested():
            session.add(
                UserArticle(
                    user_id=user_id,
                    article_id=article_id,
                    origin=ArticleOrigin.MANUAL.value,
                    interest_weight=MANUAL_REGISTRATION_INTEREST_WEIGHT,
                )
            )
    except IntegrityError:
        pass


def make_fetch_article_handler(settings: Settings | None = None) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `fetch_article` ハンドラを作る。"""
    resolved_settings = settings or get_settings()

    async def _handle(context: JobContext) -> None:
        await run_job_in_thread(context, resolved_settings, process_fetch_article)

    return _handle
