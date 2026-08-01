"""`fetch_article` ジョブハンドラ（`PROJECT_SPEC.md` §6.2, Issue #12 T3, Issue #9 T15）。

URL から記事を取得・保存する。payload に `registration_id` があれば
ユーザーの明示的な URL 登録として扱い、手動登録の関心記事として
`user_articles` へ追加する。`registration_id` が無ければ巡回
（`crawl_sources`）由来のジョブとして扱い、登録行の更新は一切行わない
（`_process_without_registration` 参照）。どちらの経路でも最終的に
`analyze_article` を積む。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db.enums import ArticleOrigin, JobType
from techradar.db.models import UserArticle
from techradar.fetcher.service import ingest_article
from techradar.jobs.handlers._shared import (
    load_registration,
    record_registration_failure_safely,
    run_job_in_thread,
    start_registration_step,
)
from techradar.jobs.handlers.errors import classify_fetch_error
from techradar.jobs.queue import enqueue
from techradar.jobs.registry import JobContext, JobHandler

# 手動 URL 登録の関心重み（`PROJECT_SPEC.md` §7.1）。
MANUAL_REGISTRATION_INTEREST_WEIGHT = 1.0


def process_fetch_article(session: Session, context: JobContext, settings: Settings) -> None:
    """`fetch_article` ジョブ 1 件分の処理。"""
    url = context.payload["url"]
    registration_id = _optional_registration_id(context.payload)

    if registration_id is None:
        # 巡回由来のジョブ。登録行が無いため、以降の登録更新・失敗記録はすべて
        # 省略する（`_process_without_registration` のコメント参照）。
        _process_without_registration(session, url, settings)
        return

    registration = load_registration(session, registration_id)
    if registration is None:
        return

    start_registration_step(session, registration, JobType.FETCH_ARTICLE)

    # 分類済みの例外だけでなく想定外の例外も記録する。記録しないまま抜けると
    # 登録が実行中 status・理由なしのまま残り、UI からは永久に処理中に見える。
    try:
        result = ingest_article(session, url, settings=settings)

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
    except Exception as exc:
        record_registration_failure_safely(
            session,
            registration_id,
            classify_fetch_error(exc),
            context=context,
            settings=settings,
        )
        raise


def _process_without_registration(session: Session, url: str, settings: Settings) -> None:
    """登録行を伴わない（巡回由来の）`fetch_article` を処理する。

    手動登録との違い:
    - `user_articles` へは追加しない。手動登録（`origin=manual`, 重み 1.0）は
      ユーザーの明示的な関心表明を表すが、巡回で機械的に見つけただけの候補に
      同じ重みを与えると実際の関心度を過大評価してしまうため（受入基準）。
    - 失敗理由の記録先である登録行が無いため `record_registration_failure_safely`
      は呼ばない。例外はここで握りつぶさずそのまま再送出し、ジョブ自体の失敗
      記録（`techradar.jobs.queue.fail` が書き込む `jobs.last_error`）に委ねる。
    """
    result = ingest_article(session, url, settings=settings)
    enqueue(session, JobType.ANALYZE_ARTICLE, {"article_id": str(result.article.id)})
    session.flush()


def _optional_registration_id(payload: dict[str, Any]) -> uuid.UUID | None:
    """payload から `registration_id` を取り出す。無ければ巡回由来として `None`。"""
    raw = payload.get("registration_id")
    if raw is None:
        return None
    return uuid.UUID(raw)


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
