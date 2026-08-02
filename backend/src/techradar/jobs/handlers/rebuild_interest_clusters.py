"""`rebuild_interest_clusters` ジョブハンドラ（`PROJECT_SPEC.md` §8, Issue #15 段階 3）。

Good/保存/Bad フィードバックのたびに全関心記事の embedding をクラスタリングすると
API 応答が遅くなるため、`api/feedback.py` はこのジョブを積むだけに留め、実際の
再構築（`interest.service.rebuild_interest_clusters`）はここで非同期に行う。
`ArticleRegistration` を伴わない保守寄りの処理のため、`purge_operation_logs` /
`purge_recommendation_runs` と同じ、より単純なハンドラの流儀に倣う
（`registration_id` の有無を見る 3 ハンドラの流儀とは異なる）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.interest.service import rebuild_interest_clusters
from techradar.jobs.handlers._shared import run_job_in_thread
from techradar.jobs.registry import JobContext, JobHandler

logger = logging.getLogger(__name__)


def process_rebuild_interest_clusters(
    session: Session, context: JobContext, settings: Settings
) -> None:
    """`rebuild_interest_clusters` ジョブ 1 件分の処理。

    payload の `user_id` は UUID として検証する。存在しない・型が不正な場合は
    `KeyError` / `ValueError` をそのまま送出し、既存ハンドラ
    （`jobs/handlers/embed_article.py` の `article_id` の扱い等）と同じく
    ワーカー側のリトライ・失敗記録に委ねる。
    """
    del settings  # 現時点では未使用（呼び出し側との引数統一のために残す）。
    user_id = uuid.UUID(context.payload["user_id"])
    now = datetime.now(UTC)

    cluster_count = rebuild_interest_clusters(session, user_id, now)
    logger.info(
        "rebuild_interest_clusters.completed job_id=%s user_id=%s cluster_count=%s",
        context.job_id,
        user_id,
        cluster_count,
    )


def make_rebuild_interest_clusters_handler(settings: Settings | None = None) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `rebuild_interest_clusters` ハンドラを作る。"""
    resolved_settings = settings or get_settings()

    async def _handle(context: JobContext) -> None:
        await run_job_in_thread(context, resolved_settings, process_rebuild_interest_clusters)

    return _handle
