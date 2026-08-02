"""URL 登録・巡回のジョブハンドラ群（Issue #12 T3, Issue #9 T14）。

`fetch_article` → `analyze_article` → `embed_article` の順に、前段のハンドラが
次段のジョブを積む形で連鎖する（`PROJECT_SPEC.md` §6.2 の状態遷移）。
`crawl_sources` はこの連鎖の起点で、巡回で見つけた候補記事を `fetch_article`
として積む（`registration_id` を持たない、ユーザーの明示的な URL 登録を
経由しない経路）。
"""

from __future__ import annotations

from techradar.jobs.handlers.analyze_article import make_analyze_article_handler
from techradar.jobs.handlers.crawl_sources import make_crawl_sources_handler
from techradar.jobs.handlers.embed_article import make_embed_article_handler
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.handlers.fetch_article import make_fetch_article_handler
from techradar.jobs.handlers.purge_operation_logs import make_purge_operation_logs_handler
from techradar.jobs.handlers.purge_recommendation_runs import (
    make_purge_recommendation_runs_handler,
)

__all__ = [
    "RegistrationErrorReason",
    "make_analyze_article_handler",
    "make_crawl_sources_handler",
    "make_embed_article_handler",
    "make_fetch_article_handler",
    "make_purge_operation_logs_handler",
    "make_purge_recommendation_runs_handler",
]
