"""情報源の分類と公式ソースレジストリ（`PROJECT_SPEC.md` §10, §11）。"""

from techradar.sources.classifier import classify
from techradar.sources.config import (
    RegistryConfig,
    SourceConfigError,
    get_registry_config,
    load_registry_config,
)
from techradar.sources.fallback import FallbackConfig, classify_fallback
from techradar.sources.rules import (
    SourceClassification,
    SourceRule,
    classify_url,
    is_primary_source,
    tier_of,
)
from techradar.sources.service import (
    SeedResult,
    classify_with_registry,
    load_rules,
    seed_source_registry,
)
from techradar.sources.weights import AuthorityWeights

__all__ = [
    "AuthorityWeights",
    "FallbackConfig",
    "RegistryConfig",
    "SeedResult",
    "SourceClassification",
    "SourceConfigError",
    "SourceRule",
    "classify",
    "classify_fallback",
    "classify_url",
    "classify_with_registry",
    "get_registry_config",
    "is_primary_source",
    "load_registry_config",
    "load_rules",
    "seed_source_registry",
    "tier_of",
]
