"""重複判定の設定ファイル読み込み（`PROJECT_SPEC.md` §17, §24, §25）。

閾値・減点・コスト管理のパラメータをコードに埋め込まず `config/dedup.yaml` で
管理する。読み込み時に Pydantic で検証し、壊れた設定のまま起動しないようにする。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from techradar.db.enums import ContentType
from techradar.dedup.rules import DuplicatePenalties, DuplicateThresholds, UniqueValueSettings

# backend/src/techradar/dedup/config.py から 3 階層上が backend/
BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BACKEND_ROOT / "config" / "dedup.yaml"

# 独自価値判定で 1 クラスタあたりに残す候補数の下限。0 以下だと LLM に何も問えない。
MIN_CANDIDATES_PER_CLUSTER = 1


class DedupConfigError(Exception):
    """設定ファイルを読み込めなかった場合のエラー。"""


class ThresholdsConfig(BaseModel):
    """重複と判定する類似度の閾値。"""

    model_config = ConfigDict(extra="forbid")

    title_similarity: float = Field(ge=0.0, le=1.0)
    embedding_similarity: float = Field(ge=0.0, le=1.0)


class PenaltiesConfig(BaseModel):
    """一致した段に応じた減点。"""

    model_config = ConfigDict(extra="forbid")

    canonical_url: float = Field(ge=0.0, le=1.0)
    normalized_url: float = Field(ge=0.0, le=1.0)
    body_hash: float = Field(ge=0.0, le=1.0)
    title: float = Field(ge=0.0, le=1.0)
    embedding: float = Field(ge=0.0, le=1.0)


class UniqueValueConfig(BaseModel):
    """独自価値判定に回す候補を絞る設定（コスト管理、`PROJECT_SPEC.md` §24）。"""

    model_config = ConfigDict(extra="forbid")

    content_types: list[ContentType] = Field(min_length=1)
    min_technical_quality: float = Field(ge=0.0, le=1.0)
    max_authority_gap: float = Field(ge=0.0, le=1.0)
    max_candidates_per_cluster: int = Field(ge=MIN_CANDIDATES_PER_CLUSTER)


class DedupConfig(BaseModel):
    """`config/dedup.yaml` 全体。"""

    model_config = ConfigDict(extra="forbid")

    thresholds: ThresholdsConfig
    penalties: PenaltiesConfig
    unique_value: UniqueValueConfig

    def to_thresholds(self) -> DuplicateThresholds:
        """判定に使う閾値へ変換する。"""
        return DuplicateThresholds(
            title_similarity=self.thresholds.title_similarity,
            embedding_similarity=self.thresholds.embedding_similarity,
        )

    def to_penalties(self) -> DuplicatePenalties:
        """判定に使う減点へ変換する。"""
        return DuplicatePenalties(
            canonical_url=self.penalties.canonical_url,
            normalized_url=self.penalties.normalized_url,
            body_hash=self.penalties.body_hash,
            title=self.penalties.title,
            embedding=self.penalties.embedding,
        )

    def to_unique_value_settings(self) -> UniqueValueSettings:
        """独自価値判定に使う設定へ変換する。"""
        return UniqueValueSettings(
            content_types=tuple(self.unique_value.content_types),
            min_technical_quality=self.unique_value.min_technical_quality,
            max_authority_gap=self.unique_value.max_authority_gap,
            max_candidates_per_cluster=self.unique_value.max_candidates_per_cluster,
        )


def load_dedup_config(path: Path | None = None) -> DedupConfig:
    """設定ファイルを読み込んで検証する。

    Raises:
        DedupConfigError: ファイルが無い、YAML として壊れている、
            またはマッピング以外が書かれている場合。
    """
    resolved = path or DEFAULT_CONFIG_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"重複判定設定を読み込めません: {resolved}"
        raise DedupConfigError(message) from exc

    try:
        # 任意のオブジェクトを構築しないよう safe_load を使う。
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        message = f"重複判定設定の YAML が不正です: {resolved}"
        raise DedupConfigError(message) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        message = f"重複判定設定はマッピングである必要があります: {resolved}"
        raise DedupConfigError(message)

    return DedupConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_dedup_config() -> DedupConfig:
    """同梱設定のシングルトンを返す。

    設定ファイルは起動中に変わらないため、記事 1 件ごとに読み直さない。
    テストで差し替える場合は `get_dedup_config.cache_clear()` を呼ぶ。
    """
    return load_dedup_config()
