"""公式ソースレジストリの設定ファイル読み込み（`PROJECT_SPEC.md` §11, §25）。

レジストリはコードに埋め込まず `config/sources.yaml` で管理する。
読み込み時に Pydantic で検証し、壊れた設定のまま起動しないようにする。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from techradar.db.enums import SourceType
from techradar.sources.fallback import FallbackConfig
from techradar.sources.rules import SourceRule
from techradar.sources.weights import AuthorityWeights

# backend/src/techradar/sources/config.py から 3 階層上が backend/
BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BACKEND_ROOT / "config" / "sources.yaml"


class SourceConfigError(Exception):
    """設定ファイルを読み込めなかった場合のエラー。"""


class RuleConfig(BaseModel):
    """レジストリ 1 件分の設定。"""

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1)
    path: str | None = None
    github_org: str | None = None
    type: SourceType
    # 省略時は種別ごとの既定値を使う。
    authority: float | None = Field(default=None, ge=0.0, le=1.0)


class EntityConfig(BaseModel):
    """企業・OSS 1 件分の設定。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    rules: list[RuleConfig] = Field(min_length=1)


class FallbackSettings(BaseModel):
    """未登録ドメインの推定設定。"""

    model_config = ConfigDict(extra="forbid")

    default_source_type: SourceType = SourceType.UNKNOWN
    host_prefixes: list[str] = Field(default_factory=list)
    path_hints: list[str] = Field(default_factory=list)
    domains: dict[str, SourceType] = Field(default_factory=dict)


class RegistryConfig(BaseModel):
    """`config/sources.yaml` 全体。"""

    model_config = ConfigDict(extra="forbid")

    authority_by_source_type: dict[SourceType, float] = Field(default_factory=dict)
    fallback: FallbackSettings = Field(default_factory=FallbackSettings)
    entities: list[EntityConfig] = Field(default_factory=list)

    def to_rules(self) -> tuple[SourceRule, ...]:
        """判定に使う規則へ変換する。

        `authority` の指定が無い規則には種別ごとの既定値を当てる。
        """
        weights = self.to_weights()
        return tuple(
            SourceRule(
                entity_name=entity.name,
                domain=rule.domain,
                source_type=rule.type,
                authority_score=(
                    rule.authority if rule.authority is not None else weights.score_for(rule.type)
                ),
                path_pattern=rule.path,
                github_org=rule.github_org,
            )
            for entity in self.entities
            for rule in entity.rules
        )

    def to_weights(self) -> AuthorityWeights:
        """authority スコアの重みへ変換する。"""
        return AuthorityWeights(dict(self.authority_by_source_type))

    def to_fallback_config(self) -> FallbackConfig:
        """未登録ドメインの推定設定へ変換する。"""
        return FallbackConfig(
            domains=dict(self.fallback.domains),
            host_prefixes=tuple(self.fallback.host_prefixes),
            path_hints=tuple(self.fallback.path_hints),
            default_source_type=self.fallback.default_source_type,
        )


def load_registry_config(path: Path | None = None) -> RegistryConfig:
    """設定ファイルを読み込んで検証する。

    Raises:
        SourceConfigError: ファイルが無い、YAML として壊れている、
            またはマッピング以外が書かれている場合。
    """
    resolved = path or DEFAULT_CONFIG_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"ソースレジストリ設定を読み込めません: {resolved}"
        raise SourceConfigError(message) from exc

    try:
        # 任意のオブジェクトを構築しないよう safe_load を使う。
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        message = f"ソースレジストリ設定の YAML が不正です: {resolved}"
        raise SourceConfigError(message) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        message = f"ソースレジストリ設定はマッピングである必要があります: {resolved}"
        raise SourceConfigError(message)

    return RegistryConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_registry_config() -> RegistryConfig:
    """同梱設定のシングルトンを返す。

    設定ファイルは起動中に変わらないため、記事 1 件ごとに読み直さない。
    テストで差し替える場合は `get_registry_config.cache_clear()` を呼ぶ。
    """
    return load_registry_config()
