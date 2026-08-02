"""`purge_recommendation_runs` ジョブハンドラ（`PROJECT_SPEC.md` §24 可観測性、Issue #28）。

`recommendation_runs` の保持期間は `config.py` の `recommendation_run_retention_days`
（既定 30 日）だが、`purge_operation_logs`（Issue #19）と同様に実際に削除する
実行主体が無かった。常駐スケジューラを置かない設計のため、`crawl_sources` の
完了時に積まれるジョブとしてこの削除を担う。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Delete, delete
from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db.models import RecommendationRun
from techradar.jobs.handlers._shared import run_job_in_thread
from techradar.jobs.registry import JobContext, JobHandler

logger = logging.getLogger(__name__)


def build_expired_runs_delete(cutoff: datetime) -> Delete:
    """`cutoff` より古い `recommendation_runs` を消す DELETE 文を組み立てる。

    `purge_expired_recommendation_runs` から分離しているのは、実行計画を検証する
    テストが実際に発行される文と同じものを見られるようにするため（Issue #32）。
    """
    return delete(RecommendationRun).where(RecommendationRun.generated_at < cutoff)


def purge_expired_recommendation_runs(
    session: Session, *, retention_days: int, now: datetime | None = None
) -> int:
    """保持期間を超えた `recommendation_runs` を削除し、削除件数を返す。

    境界は `generated_at < cutoff` にする。ちょうど cutoff の行は「保持期間を
    超えた」とは言えないため残す。

    紐づく `recommendations` は `recommendations.run_id` の `ondelete="CASCADE"`
    により DB 側で連鎖削除されるため、ここで別途削除する必要はない。

    対象件数は運用期間に比例して増えうるため、ORM で1行ずつ削除せず DELETE 文
    1本で処理する（`techradar.jobs.queue.reclaim_stale` と同じ方針）。
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    stmt = build_expired_runs_delete(cutoff)
    # DELETE は ORM の Unit of Work を経由しないため、未送信の変更があると
    # それを取りこぼす。先に flush して DB 側の状態を揃えてから削除する。
    session.flush()
    # Session.execute() の戻り値型は Result[Any] で rowcount を持たない。
    # Connection.execute() は常に CursorResult を返すため、セッションの現在の
    # トランザクションに紐づく接続を明示的に使う（`reclaim_stale` と同じ）。
    result = session.connection().execute(stmt)
    # 削除で消えた行を ORM が古いまま参照し続けないよう、Identity Map を捨てる。
    session.expire_all()
    return result.rowcount


def process_purge_recommendation_runs(
    session: Session, context: JobContext, settings: Settings
) -> None:
    """`purge_recommendation_runs` ジョブ 1 件分の処理。

    保持日数は `settings.recommendation_run_retention_days`（既定 30）に従う。
    ジョブの payload では上書きしない。保持期間はプロジェクト全体の方針であり、
    個々のジョブが任意の値を持ち込めると、積まれた経路によって削除範囲が
    変わってしまうため。
    """
    deleted = purge_expired_recommendation_runs(
        session, retention_days=settings.recommendation_run_retention_days
    )
    logger.info(
        "purge_recommendation_runs.deleted job_id=%s count=%s retention_days=%s",
        context.job_id,
        deleted,
        settings.recommendation_run_retention_days,
    )


def make_purge_recommendation_runs_handler(settings: Settings | None = None) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `purge_recommendation_runs` ハンドラを作る。"""
    resolved_settings = settings or get_settings()

    async def _handle(context: JobContext) -> None:
        await run_job_in_thread(context, resolved_settings, process_purge_recommendation_runs)

    return _handle
