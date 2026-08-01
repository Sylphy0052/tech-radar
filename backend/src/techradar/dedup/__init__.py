"""記事の重複判定（`PROJECT_SPEC.md` §17）。"""

from techradar.dedup.config import (
    DedupConfig,
    DedupConfigError,
    get_dedup_config,
    load_dedup_config,
)
from techradar.dedup.rules import (
    ArticleCluster,
    ArticleSignature,
    DuplicateMatch,
    DuplicatePenalties,
    DuplicateThresholds,
    MatchMethod,
    UniqueValueSettings,
    cluster_articles,
    cosine_similarity,
    duplicate_penalty_for,
    find_duplicate_match,
    normalize_title,
    select_representative,
    title_similarity,
    unique_value_candidates,
)

__all__ = [
    "ArticleCluster",
    "ArticleSignature",
    "DedupConfig",
    "DedupConfigError",
    "DuplicateMatch",
    "DuplicatePenalties",
    "DuplicateThresholds",
    "MatchMethod",
    "UniqueValueSettings",
    "cluster_articles",
    "cosine_similarity",
    "duplicate_penalty_for",
    "find_duplicate_match",
    "get_dedup_config",
    "load_dedup_config",
    "normalize_title",
    "select_representative",
    "title_similarity",
    "unique_value_candidates",
]
