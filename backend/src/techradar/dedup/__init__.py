"""記事の重複判定（`PROJECT_SPEC.md` §17）。"""

from techradar.dedup.config import (
    DedupConfig,
    DedupConfigError,
    get_dedup_config,
    load_dedup_config,
)
from techradar.dedup.judge import (
    UNIQUE_VALUE_INSTRUCTION,
    UniqueValueJudgment,
    judge_unique_value,
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
from techradar.dedup.service import DeduplicationResult, deduplicate_articles

__all__ = [
    "UNIQUE_VALUE_INSTRUCTION",
    "ArticleCluster",
    "ArticleSignature",
    "DedupConfig",
    "DedupConfigError",
    "DeduplicationResult",
    "DuplicateMatch",
    "DuplicatePenalties",
    "DuplicateThresholds",
    "MatchMethod",
    "UniqueValueJudgment",
    "UniqueValueSettings",
    "cluster_articles",
    "cosine_similarity",
    "deduplicate_articles",
    "duplicate_penalty_for",
    "find_duplicate_match",
    "get_dedup_config",
    "judge_unique_value",
    "load_dedup_config",
    "normalize_title",
    "select_representative",
    "title_similarity",
    "unique_value_candidates",
]
