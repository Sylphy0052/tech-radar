"""レジストリと推定を組み合わせた情報源の判定（`PROJECT_SPEC.md` §10, §11）。

レジストリに載っていれば公式扱いし、載っていなければ推定へ落とす。
どちらも純粋関数のため、DB を使わずに検証できる（`PROJECT_SPEC.md` §25）。
"""

from __future__ import annotations

from techradar.db.enums import SourceType
from techradar.sources.fallback import FallbackConfig, classify_fallback
from techradar.sources.rules import SourceClassification, SourceRule, classify_url
from techradar.sources.weights import AuthorityWeights


def classify(
    url: str,
    rules: tuple[SourceRule, ...],
    fallback: FallbackConfig,
    weights: AuthorityWeights,
) -> SourceClassification:
    """URL の情報源種別と authority を決める。"""
    matched = classify_url(url, rules)
    if matched.source_type is not SourceType.UNKNOWN:
        return matched
    return classify_fallback(url, fallback, weights)
