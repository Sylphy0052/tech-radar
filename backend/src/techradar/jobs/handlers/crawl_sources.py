"""`crawl_sources` ジョブハンドラ（`PROJECT_SPEC.md` §12, Issue #9 T14）。

巡回設定に従ってコレクター群を実行し、見つかった候補記事を `fetch_article`
ジョブとして積む。実際の収集・絞り込みロジックは
`techradar.collectors.service.collect_candidates` に委ね、ここではジョブと
しての配線（別スレッドへ逃がす・payload の解釈）だけを担う。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from techradar.collectors.service import collect_candidates
from techradar.config import Settings, get_settings
from techradar.db.enums import JobType
from techradar.jobs.handlers._shared import run_job_in_thread
from techradar.jobs.queue import enqueue
from techradar.jobs.registry import JobContext, JobHandler

logger = logging.getLogger(__name__)


def process_crawl_sources(session: Session, context: JobContext, settings: Settings) -> None:
    """`crawl_sources` ジョブ 1 件分の処理。

    セキュリティ上の設計判断（MR !9 のレビュー申し送りに対する受入基準）:
    `payload["source_domain"]` は API 側（`techradar.api.crawl`）では形式検証しか
    行っておらず、`169.254.169.254` のような IP リテラルやリンクローカル
    アドレスが入りうる。この値から URL を組み立てて直接リクエストを送る経路は
    絶対に作らない。ここでは `source_domain` を
    `techradar.collectors.service.collect_candidates` へそのまま渡し、
    「巡回結果を後から絞り込むフィルタ条件」としてのみ使う。実際の HTTP
    通信は各コレクターが既定の巡回先（設定済みフィード・API）に対して
    `techradar.fetcher.fetch_resource` / `fetch_page` 経由でのみ行うため、
    `source_domain` の値が SSRF の入力として使われることはない。
    """
    collect_candidates(session, settings=settings, source_domain=_source_domain(context.payload))
    _enqueue_log_purge(session)
    _enqueue_recommendation_run_purge(session)


def _enqueue_log_purge(session: Session) -> None:
    """保持期間を過ぎた `operation_logs` の削除ジョブを積む（Issue #19）。

    常駐スケジューラを置かない設計のため、定期実行の契機は UI の巡回ボタンしか
    ない。巡回が実際に走ったときにここで積むことで、保持期間 90 日
    （`PROJECT_SPEC.md` §24）に実行主体を与える。

    候補が 0 件でもログの保持期間は経過するため、収集結果に関わらず積む。
    API 側の重複起動抑制（`api/crawl.py`）が防ぐのは巡回ジョブの同時起動までで、
    `reclaim_stale` による巡回自体の再実行までは防げない。その場合は削除ジョブが
    重複して積まれるが、削除は同じ条件の DELETE を繰り返すだけで冪等なため
    （2 回目以降は対象 0 件になる）、ここでは重複を許容する。
    """
    enqueue(session, JobType.PURGE_OPERATION_LOGS)


def _enqueue_recommendation_run_purge(session: Session) -> None:
    """保持期間を過ぎた `recommendation_runs` の削除ジョブを積む（Issue #28）。

    `_enqueue_log_purge` と同じ理由（常駐スケジューラを置かない設計）で、
    巡回が実際に走ったときにここで積む。候補が 0 件でも run の保持期間は
    経過するため、収集結果に関わらず積む。重複起動時の扱いも
    `_enqueue_log_purge` と同じで、削除は冪等なため重複を許容する。
    """
    enqueue(session, JobType.PURGE_RECOMMENDATION_RUNS)


def _source_domain(payload: dict[str, Any]) -> str | None:
    """payload から `source_domain` を取り出す。

    `Job.payload` は JSONB のため、API を経ずに積まれたジョブでは文字列以外
    （list / int / dict）が入りうる。そのまま渡すと絞り込み側の文字列操作が
    `AttributeError` で落ち、原因の分からない失敗としてジョブに記録される。
    境界は API 層だけでなくジョブ実行層にも置き、型が違えば「絞り込み指定なし」
    として扱う（巡回自体は続行してよいため、例外にはしない）。
    """
    raw = payload.get("source_domain")
    if not isinstance(raw, str):
        if raw is not None:
            logger.warning("crawl_sources.invalid_source_domain type=%s", type(raw).__name__)
        return None
    return raw


def make_crawl_sources_handler(settings: Settings | None = None) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `crawl_sources` ハンドラを作る。"""
    resolved_settings = settings or get_settings()

    async def _handle(context: JobContext) -> None:
        await run_job_in_thread(context, resolved_settings, process_crawl_sources)

    return _handle
