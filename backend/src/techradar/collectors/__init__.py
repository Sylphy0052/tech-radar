"""候補記事コレクターの抽象・巡回設定・巡回サービス（`PROJECT_SPEC.md` §12）。"""

from techradar.collectors.base import CandidateArticle, CollectorError, SourceCollector
from techradar.collectors.config import (
    CollectorConfigError,
    FeedEntryConfig,
    FeedsConfig,
    get_feeds_config,
    load_feeds_config,
)
from techradar.collectors.service import CollectResult, collect_candidates

__all__ = [
    "CandidateArticle",
    "CollectResult",
    "CollectorConfigError",
    "CollectorError",
    "FeedEntryConfig",
    "FeedsConfig",
    "SourceCollector",
    "collect_candidates",
    "get_feeds_config",
    "load_feeds_config",
]
