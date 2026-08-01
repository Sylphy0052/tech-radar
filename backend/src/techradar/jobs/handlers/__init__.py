"""URL 登録の end-to-end を成立させるジョブハンドラ群（Issue #12 T3）。

`fetch_article` → `analyze_article` → `embed_article` の順に、前段のハンドラが
次段のジョブを積む形で連鎖する（`PROJECT_SPEC.md` §6.2 の状態遷移）。
"""

from __future__ import annotations

from techradar.jobs.handlers.analyze_article import make_analyze_article_handler
from techradar.jobs.handlers.embed_article import make_embed_article_handler
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.handlers.fetch_article import make_fetch_article_handler

__all__ = [
    "RegistrationErrorReason",
    "make_analyze_article_handler",
    "make_embed_article_handler",
    "make_fetch_article_handler",
]
