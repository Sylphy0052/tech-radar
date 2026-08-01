"""`crawl_sources` ジョブハンドラ（`PROJECT_SPEC.md` §12, Issue #9 T14）。

巡回設定に従ってコレクター群を実行し、見つかった候補記事を `fetch_article`
ジョブとして積む。実際の収集・絞り込みロジックは
`techradar.collectors.service.collect_candidates` に委ね、ここではジョブと
しての配線（別スレッドへ逃がす・payload の解釈）だけを担う。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from techradar.collectors.service import collect_candidates
from techradar.config import Settings, get_settings
from techradar.jobs.handlers._shared import run_job_in_thread
from techradar.jobs.registry import JobContext, JobHandler


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
    source_domain = context.payload.get("source_domain")
    collect_candidates(session, settings=settings, source_domain=source_domain)


def make_crawl_sources_handler(settings: Settings | None = None) -> JobHandler:
    """`JobHandlerRegistry` へ登録する `crawl_sources` ハンドラを作る。"""
    resolved_settings = settings or get_settings()

    async def _handle(context: JobContext) -> None:
        await run_job_in_thread(context, resolved_settings, process_crawl_sources)

    return _handle
