"""推薦スコアの設定ファイル読み込み（`PROJECT_SPEC.md` §14, §15, §25）。

重み・減点・フィード構成比のパラメータをコードに埋め込まず `config/scoring.yaml`
で管理する。読み込み時に Pydantic で検証し、壊れた設定を検出する。

`get_scoring_config()` は `lru_cache` 付きの遅延読み込みのため、この検証が
実際に走るのはプロセス起動時ではなく、推薦処理が初めて呼ばれたタイミングである
（起動時ヘルスチェックとしての導入はこの MR のスコープ外、`dedup/config.py` と
同じ方針）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from techradar.recommendation.ranking import (
    AuthorityGate,
    FeedComposition,
    FreshnessSettings,
    InterestSettings,
    MatchSettings,
    NoveltySettings,
    RankingLimits,
    ScorePenalties,
    ScoreWeights,
    ScoringSettings,
)

# backend/src/techradar/recommendation/config.py から 3 階層上が backend/
BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BACKEND_ROOT / "config" / "scoring.yaml"

# 浮動小数点の丸め誤差を吸収する許容差（重み・構成比の合計が 1.0 かの検証に使う）。
_SUM_TOLERANCE = 1e-6

# 関心一致度の上位何件を平均するかの下限。0 以下だと平均を取れない。
MIN_INTEREST_TOP_K = 1

# フィード 1 ページあたりの件数の下限。0 以下だと何も返せない。
MIN_PAGE_SIZE = 1

# 1 回の実行で採点する候補数の下限。0 以下だと何も処理できない。
MIN_CANDIDATES_PER_RUN = 1


class ScoringConfigError(Exception):
    """設定ファイルを読み込めなかった場合のエラー。"""


class WeightsConfig(BaseModel):
    """`recommendation_score` を構成する各項目の重み。合計は 1.0 にする。"""

    model_config = ConfigDict(extra="forbid")

    interest_similarity: float = Field(ge=0.0, le=1.0)
    source_authority: float = Field(ge=0.0, le=1.0)
    source_article_match: float = Field(ge=0.0, le=1.0)
    freshness: float = Field(ge=0.0, le=1.0)
    technical_quality: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)


class PenaltiesConfig(BaseModel):
    """候補の状態に応じた固定減点。"""

    model_config = ConfigDict(extra="forbid")

    bad: float = Field(ge=0.0, le=1.0)
    read: float = Field(ge=0.0, le=1.0)


class AuthorityGateConfig(BaseModel):
    """`source_authority` の寄与に掛けるゲートの設定（`PROJECT_SPEC.md` §14 補足）。"""

    model_config = ConfigDict(extra="forbid")

    min_interest_similarity: float = Field(ge=0.0, le=1.0)
    min_factor: float = Field(ge=0.0, le=1.0)


class FreshnessConfig(BaseModel):
    """新しさによる減衰の設定。"""

    model_config = ConfigDict(extra="forbid")

    max_age_days: float = Field(gt=0.0)


class InterestConfig(BaseModel):
    """関心一致度（`interest_similarity`）の計算設定。"""

    model_config = ConfigDict(extra="forbid")

    top_k: int = Field(ge=MIN_INTEREST_TOP_K)


class SourceMatchConfig(BaseModel):
    """情報源と記事主題の一致度（`source_article_match`）の計算設定。"""

    model_config = ConfigDict(extra="forbid")

    partial_match_score: float = Field(ge=0.0, le=1.0)


class NoveltyConfig(BaseModel):
    """新規性（`novelty`）の計算設定。"""

    model_config = ConfigDict(extra="forbid")

    default_when_no_topics: float = Field(ge=0.0, le=1.0)


class FeedCompositionConfig(BaseModel):
    """Discover フィードの構成比の目安（`PROJECT_SPEC.md` §15）。合計は 1.0 にする。"""

    model_config = ConfigDict(extra="forbid")

    strong_interest: float = Field(ge=0.0, le=1.0)
    primary_source: float = Field(ge=0.0, le=1.0)
    exploration: float = Field(ge=0.0, le=1.0)
    diversity: float = Field(ge=0.0, le=1.0)


class LimitsConfig(BaseModel):
    """ランキング処理の安全弁とページングの既定値。"""

    model_config = ConfigDict(extra="forbid")

    max_candidates_per_run: int = Field(ge=MIN_CANDIDATES_PER_RUN)
    default_page_size: int = Field(ge=MIN_PAGE_SIZE)
    max_page_size: int = Field(ge=MIN_PAGE_SIZE)

    @model_validator(mode="after")
    def _validate_default_within_max(self) -> LimitsConfig:
        if self.default_page_size > self.max_page_size:
            message = (
                "default_page_size は max_page_size 以下である必要があります: "
                f"default_page_size={self.default_page_size}, max_page_size={self.max_page_size}"
            )
            raise ValueError(message)
        return self


class ScoringConfig(BaseModel):
    """`config/scoring.yaml` 全体。"""

    model_config = ConfigDict(extra="forbid")

    weights: WeightsConfig
    penalties: PenaltiesConfig
    authority_gate: AuthorityGateConfig
    freshness: FreshnessConfig
    interest: InterestConfig
    source_match: SourceMatchConfig
    novelty: NoveltyConfig
    feed_composition: FeedCompositionConfig
    limits: LimitsConfig

    @model_validator(mode="after")
    def _validate_weights_sum_to_one(self) -> ScoringConfig:
        total = (
            self.weights.interest_similarity
            + self.weights.source_authority
            + self.weights.source_article_match
            + self.weights.freshness
            + self.weights.technical_quality
            + self.weights.novelty
        )
        if abs(total - 1.0) > _SUM_TOLERANCE:
            message = f"weights の合計は 1.0 である必要があります（現在: {total}）"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_feed_composition_sums_to_one(self) -> ScoringConfig:
        total = (
            self.feed_composition.strong_interest
            + self.feed_composition.primary_source
            + self.feed_composition.exploration
            + self.feed_composition.diversity
        )
        if abs(total - 1.0) > _SUM_TOLERANCE:
            message = f"feed_composition の合計は 1.0 である必要があります（現在: {total}）"
            raise ValueError(message)
        return self

    def to_settings(self) -> ScoringSettings:
        """採点に使う `ScoringSettings` へ変換する。"""
        return ScoringSettings(
            weights=ScoreWeights(
                interest_similarity=self.weights.interest_similarity,
                source_authority=self.weights.source_authority,
                source_article_match=self.weights.source_article_match,
                freshness=self.weights.freshness,
                technical_quality=self.weights.technical_quality,
                novelty=self.weights.novelty,
            ),
            penalties=ScorePenalties(bad=self.penalties.bad, read=self.penalties.read),
            authority_gate=AuthorityGate(
                min_interest_similarity=self.authority_gate.min_interest_similarity,
                min_factor=self.authority_gate.min_factor,
            ),
            freshness=FreshnessSettings(max_age_days=self.freshness.max_age_days),
            interest=InterestSettings(top_k=self.interest.top_k),
            source_match=MatchSettings(partial_match_score=self.source_match.partial_match_score),
            novelty=NoveltySettings(default_when_no_topics=self.novelty.default_when_no_topics),
            feed_composition=FeedComposition(
                strong_interest=self.feed_composition.strong_interest,
                primary_source=self.feed_composition.primary_source,
                exploration=self.feed_composition.exploration,
                diversity=self.feed_composition.diversity,
            ),
            limits=RankingLimits(
                max_candidates_per_run=self.limits.max_candidates_per_run,
                default_page_size=self.limits.default_page_size,
                max_page_size=self.limits.max_page_size,
            ),
        )


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    """設定ファイルを読み込んで検証する。

    Raises:
        ScoringConfigError: ファイルが無い、YAML として壊れている、
            またはマッピング以外が書かれている場合。
    """
    resolved = path or DEFAULT_CONFIG_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"推薦スコア設定を読み込めません: {resolved}"
        raise ScoringConfigError(message) from exc

    try:
        # 任意のオブジェクトを構築しないよう safe_load を使う。
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        message = f"推薦スコア設定の YAML が不正です: {resolved}"
        raise ScoringConfigError(message) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        message = f"推薦スコア設定はマッピングである必要があります: {resolved}"
        raise ScoringConfigError(message)

    return ScoringConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_scoring_config() -> ScoringConfig:
    """同梱設定のシングルトンを返す。

    設定ファイルは起動中に変わらないため、記事 1 件ごとに読み直さない。
    テストで差し替える場合は `get_scoring_config.cache_clear()` を呼ぶ。
    """
    return load_scoring_config()
