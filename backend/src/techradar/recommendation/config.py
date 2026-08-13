"""推薦スコアの設定ファイル読み込み（`PROJECT_SPEC.md` §14, §15, §25）。

重み・減点・フィード構成比のパラメータをコードに埋め込まず `config/scoring.yaml`
で管理する。読み込み時に Pydantic で検証し、壊れた設定を検出する。

`get_scoring_config()` は `lru_cache` 付きの遅延読み込みで、`dedup/config.py` と
同じ方針だが、`api/recommendations.py` がルーターのモジュールレベルで
`get_scoring_config().limits` を読むため、実際にはアプリ起動時（ルーター import
経由）に検証が走り fail-fast する。壊れた設定のまま起動できてしまう事態を防げる
ため、この挙動は意図的なものとして維持する。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from techradar.recommendation.ranking import (
    AuthorityGate,
    BadSimilaritySettings,
    FeedComposition,
    FreshnessSettings,
    InterestSettings,
    MatchSettings,
    NoveltySettings,
    RankingLimits,
    ScorePenalties,
    ScoreWeights,
    ScoringSettings,
    SourcePreferenceGate,
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

# 関心プロファイル構築対象の記事数の下限。0 以下だと関心を表現できない。
MIN_PROFILE_ARTICLES = 1

# Bad 近傍抑制で使う Bad 記事 embedding 数の下限。0 以下だと抑制を表現できない。
MIN_BAD_PROFILE_ARTICLES = 1

# トピック重み低下の判定に見る直近フィードバック件数の下限。0 以下だと判定できない。
MIN_RECENT_WINDOW = 1

# 直近フィードバック中の Bad 件数の下限。0 以下だと「Bad が一定数以上」を表せない。
MIN_BAD_THRESHOLD = 1

# 関心クラスタ数の下限。0 以下だとクラスタを構築できない。
MIN_CLUSTER_COUNT = 1

# 1 クラスタを成立させるのに要する記事数の下限。0 以下だと閾値として機能しない。
MIN_ARTICLES_PER_CLUSTER = 1

# クラスタラベルに使うトピック語数の下限。0 以下だとラベルを作れない。
MIN_LABEL_TOPIC_COUNT = 1

# KMeans の random_state の下限（scikit-learn の仕様上 0 以上の整数）。
MIN_RANDOM_STATE = 0

# 関心タイムライン（`GET /api/interests/timeline`）が返す週数の下限。
# 0 以下だと集計期間を表せない。
MIN_TIMELINE_WEEKS = 1

# 関心サマリー（`GET /api/interests/summary`）の各リスト上限件数の下限。
# 0 以下だと一覧を返せない。genres/technologies/suppressed_topics の 3 項目は
# いずれも「上限件数の下限」という同じ意味のため、`LimitsConfig` の
# `MIN_PAGE_SIZE`（複数フィールドで共有）と同じ発想で 1 つの定数にまとめる。
MIN_SUMMARY_LIMIT = 1


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
    # 関心プロファイル構築対象の記事数上限（`recommendation/service.py` が使う）。
    # ランキングの純粋関数（`ScoringSettings`）には関係しないため `InterestSettings`
    # へは変換しない。
    max_profile_articles: int = Field(ge=MIN_PROFILE_ARTICLES)
    # Bad 近傍抑制（`ranking.compute_bad_similarity_penalty`）用に読み込む Bad
    # 記事 embedding 数の上限（`recommendation/service.py` が使う）。
    # `max_profile_articles` と同じ発想の安全弁で、ユーザーの Bad 履歴が
    # 増え続けても実行のたびに全件を読み込んで計算コストが際限なく増えない
    # ようにする。ランキングの純粋関数には関係しないため `InterestSettings`
    # へは変換しない。
    max_bad_profile_articles: int = Field(ge=MIN_BAD_PROFILE_ARTICLES)


class SourceMatchConfig(BaseModel):
    """情報源と記事主題の一致度（`source_article_match`）の計算設定。"""

    model_config = ConfigDict(extra="forbid")

    partial_match_score: float = Field(ge=0.0, le=1.0)


class NoveltyConfig(BaseModel):
    """新規性（`novelty`）の計算設定。"""

    model_config = ConfigDict(extra="forbid")

    default_when_no_embedding: float = Field(ge=0.0, le=1.0)


class FeedCompositionConfig(BaseModel):
    """Discover フィードの構成比の目安（`PROJECT_SPEC.md` §15）。合計は 1.0 にする。"""

    model_config = ConfigDict(extra="forbid")

    strong_interest: float = Field(ge=0.0, le=1.0)
    primary_source: float = Field(ge=0.0, le=1.0)
    exploration: float = Field(ge=0.0, le=1.0)
    diversity: float = Field(ge=0.0, le=1.0)
    strong_interest_min_similarity: float = Field(ge=0.0, le=1.0)
    exploration_min_novelty: float = Field(ge=0.0, le=1.0)


class LimitsConfig(BaseModel):
    """ランキング処理の安全弁とページングの既定値。"""

    model_config = ConfigDict(extra="forbid")

    max_candidates_per_run: int = Field(ge=MIN_CANDIDATES_PER_RUN)
    default_page_size: int = Field(ge=MIN_PAGE_SIZE)
    max_page_size: int = Field(ge=MIN_PAGE_SIZE)
    # Discover フィード 1 回の実行で保存する件数（`recommendation/service.py` が使う）。
    # `RankingLimits` には含めない。採点・構成比の純粋関数はページングと無関係のため。
    feed_run_size: int = Field(ge=MIN_PAGE_SIZE)
    # 記事起点推薦 1 回の実行で保存する件数。
    article_based_run_size: int = Field(ge=MIN_PAGE_SIZE)
    # cursor 無しで GET /api/feed を呼んだとき、直近の DISCOVER run をこの秒数
    # 以内なら新規生成せず再利用する（`api/recommendations.py` の `get_feed`）。
    # 0 は「常に新規生成する」（無効化）を意味する。
    feed_run_reuse_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_default_within_max(self) -> LimitsConfig:
        if self.default_page_size > self.max_page_size:
            message = (
                "default_page_size は max_page_size 以下である必要があります: "
                f"default_page_size={self.default_page_size}, max_page_size={self.max_page_size}"
            )
            raise ValueError(message)
        return self


class FeedbackWeightsConfig(BaseModel):
    """フィードバック種別ごとの重み（`PROJECT_SPEC.md` §7.1, §7.2 の重み表）。

    `bad` は負方向の強さの絶対値として用意した値だが、現状はどこからも消費
    していない。`ArticleOrigin` に Bad 相当の値が無いため
    `interest/weights.py` の `explicit_weight_for_origin` からは呼ばれず、
    トピック単位の Bad 抑制は `topic_preference.decay_step` が単独で強さを
    担っている（両方を掛け合わせると二重管理になるため、意図的に未消費に
    している。詳細は `config/scoring.yaml` の `feedback_weights.bad` の
    コメント参照、Issue #15 自己レビュー 3）。符号は使う側が持ち、ここには
    負数を書かない。
    """

    model_config = ConfigDict(extra="forbid")

    manual: float = Field(ge=0.0, le=1.0)
    good: float = Field(ge=0.0, le=1.0)
    save: float = Field(ge=0.0, le=1.0)
    read_full: float = Field(ge=0.0, le=1.0)
    clicked: float = Field(ge=0.0, le=1.0)
    bad: float = Field(ge=0.0, le=1.0)


class InterestDecayConfig(BaseModel):
    """関心の時間減衰の設定（`PROJECT_SPEC.md` §8 の `recency_decay`）。"""

    model_config = ConfigDict(extra="forbid")

    half_life_days: float = Field(gt=0.0)


class ConfidenceConfig(BaseModel):
    """`effective_interest` の `confidence` の設定（`PROJECT_SPEC.md` §8、Issue #20）。

    記事について手元にある情報の充足度から確信度を決める
    （`interest/weights.py` の `compute_confidence`）。3 つのシグナルの寄与の
    合計は 1.0 にする（全て揃った記事が減衰を受けないようにするため）。
    """

    model_config = ConfigDict(extra="forbid")

    has_embedding: float = Field(ge=0.0, le=1.0)
    has_topics: float = Field(ge=0.0, le=1.0)
    is_analyzed: float = Field(ge=0.0, le=1.0)
    min_confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_signals_sum_to_one(self) -> ConfidenceConfig:
        total = self.has_embedding + self.has_topics + self.is_analyzed
        if abs(total - 1.0) > _SUM_TOLERANCE:
            message = f"confidence のシグナルの合計は 1.0 である必要があります（現在: {total}）"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_min_confidence_below_every_signal(self) -> ConfidenceConfig:
        """下限が個々のシグナルの寄与を上回らないことを確認する。

        `min_confidence` が最小のシグナルより大きいと、「シグナルが 1 つだけ
        揃った記事」と「1 つも揃っていない記事」の confidence が下限に潰れて
        同じ値になり、充足度を段階的に反映するという設計が成り立たなくなる。
        """
        smallest_signal = min(self.has_embedding, self.has_topics, self.is_analyzed)
        if self.min_confidence > smallest_signal:
            message = (
                "min_confidence は最も小さいシグナルの寄与以下である必要があります: "
                f"min_confidence={self.min_confidence}, 最小のシグナル={smallest_signal}"
            )
            raise ValueError(message)
        return self


class TopicPreferenceConfig(BaseModel):
    """トピック単位の選好更新の設定（`PROJECT_SPEC.md` §7.2）。

    「一件のBadだけでジャンル全体を抑制しない」ため、直近 `recent_window` 件中
    `bad_threshold` 件以上が Bad のときだけ段階的にトピック重みを下げる。
    """

    model_config = ConfigDict(extra="forbid")

    recent_window: int = Field(ge=MIN_RECENT_WINDOW)
    bad_threshold: int = Field(ge=MIN_BAD_THRESHOLD)
    decay_step: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_bad_threshold_within_window(self) -> TopicPreferenceConfig:
        if self.bad_threshold > self.recent_window:
            message = (
                "bad_threshold は recent_window 以下である必要があります: "
                f"bad_threshold={self.bad_threshold}, recent_window={self.recent_window}"
            )
            raise ValueError(message)
        return self


class SourcePreferenceConfig(BaseModel):
    """情報源単位の選好更新と、その推薦スコアへの反映の設定（`PROJECT_SPEC.md` §7.1 手順 4）。

    前半 3 項目（`recent_window` / `bad_threshold` / `decay_step`）は選好の更新側
    （`interest/sources.py`）が使い、後半 3 項目（`weight_scale` / `min_factor` /
    `max_factor`）は採点側（`recommendation/ranking.py` の
    `SourcePreferenceGate`）が使う。`topic_preference` と同じ値を共有せず別
    セクションにしているのは、同一ドメインの記事がトピックより頻繁に候補へ
    現れるため、閾値と低下量を独立に調整できるようにするため（Issue #34）。
    """

    model_config = ConfigDict(extra="forbid")

    recent_window: int = Field(ge=MIN_RECENT_WINDOW)
    bad_threshold: int = Field(ge=MIN_BAD_THRESHOLD)
    decay_step: float = Field(gt=0.0)
    weight_scale: float = Field(ge=0.0)
    min_factor: float = Field(ge=0.0)
    max_factor: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _validate_bad_threshold_within_window(self) -> SourcePreferenceConfig:
        if self.bad_threshold > self.recent_window:
            message = (
                "bad_threshold は recent_window 以下である必要があります: "
                f"bad_threshold={self.bad_threshold}, recent_window={self.recent_window}"
            )
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_min_within_max_factor(self) -> SourcePreferenceConfig:
        if self.min_factor > self.max_factor:
            message = (
                "min_factor は max_factor 以下である必要があります: "
                f"min_factor={self.min_factor}, max_factor={self.max_factor}"
            )
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_neutral_factor_is_reachable(self) -> SourcePreferenceConfig:
        """係数の範囲が中立（1.0）を含むことを保証する。

        `recommendation/ranking.py` の `compute_source_preference_factor` は
        選好が無い情報源（`effective_weight` が 0）に対して
        `clamp(1.0, min_factor, max_factor)` を返す。範囲が 1.0 を含まないと
        「まだ学習していない情報源のスコアは従来と変わらない」という前提が
        設定次第で崩れ、全候補の `source_authority` の寄与が一律に増減して
        しまうため、設定の時点で弾く。
        """
        if not (self.min_factor <= 1.0 <= self.max_factor):
            message = (
                "min_factor と max_factor の範囲は 1.0（選好なしの中立値）を"
                f"含む必要があります: min_factor={self.min_factor}, "
                f"max_factor={self.max_factor}"
            )
            raise ValueError(message)
        return self


class BadSimilarityConfig(BaseModel):
    """Bad 記事と意味的に近い記事を抑制する設定（`PROJECT_SPEC.md` §7.2）。"""

    model_config = ConfigDict(extra="forbid")

    min_similarity: float = Field(ge=0.0, le=1.0)
    max_penalty: float = Field(ge=0.0, le=1.0)


class ClusteringConfig(BaseModel):
    """関心クラスタ構築（KMeans）の設定（`PROJECT_SPEC.md` §8）。

    `min_clusters` は目安であって強制されない。クラスタ数は
    `min_articles_per_cluster` から賄える数を上限に決まるため
    （`interest/clusters.py` の `_cluster_count`）、記事が少ないうちは
    `min_clusters` を下回る。`max_clusters` は上限として常に効く。
    """

    model_config = ConfigDict(extra="forbid")

    min_clusters: int = Field(ge=MIN_CLUSTER_COUNT)
    max_clusters: int = Field(ge=MIN_CLUSTER_COUNT)
    min_articles_per_cluster: int = Field(ge=MIN_ARTICLES_PER_CLUSTER)
    label_topic_count: int = Field(ge=MIN_LABEL_TOPIC_COUNT)
    random_state: int = Field(ge=MIN_RANDOM_STATE)

    @model_validator(mode="after")
    def _validate_min_within_max_clusters(self) -> ClusteringConfig:
        if self.min_clusters > self.max_clusters:
            message = (
                "min_clusters は max_clusters 以下である必要があります: "
                f"min_clusters={self.min_clusters}, max_clusters={self.max_clusters}"
            )
            raise ValueError(message)
        return self


class InterestTimelineConfig(BaseModel):
    """関心タイムライン（`GET /api/interests/timeline`）の週数の既定値・上限値。"""

    model_config = ConfigDict(extra="forbid")

    default_weeks: int = Field(ge=MIN_TIMELINE_WEEKS)
    max_weeks: int = Field(ge=MIN_TIMELINE_WEEKS)

    @model_validator(mode="after")
    def _validate_default_within_max(self) -> InterestTimelineConfig:
        if self.default_weeks > self.max_weeks:
            message = (
                "default_weeks は max_weeks 以下である必要があります: "
                f"default_weeks={self.default_weeks}, max_weeks={self.max_weeks}"
            )
            raise ValueError(message)
        return self


class InterestSummaryConfig(BaseModel):
    """関心サマリー（`GET /api/interests/summary`）が返す各リストの上限件数。"""

    model_config = ConfigDict(extra="forbid")

    max_genres: int = Field(ge=MIN_SUMMARY_LIMIT)
    max_technologies: int = Field(ge=MIN_SUMMARY_LIMIT)
    max_suppressed_topics: int = Field(ge=MIN_SUMMARY_LIMIT)
    # content_type / difficulty は語彙が LLM の分類（`analysis/prompt.py`）に
    # 由来し、DB 列（`articles.content_type` / `difficulty`）自体は CHECK 制約の
    # 無い text のため、想定外の値が入っても集計が際限なく増えないよう他の
    # リストと同じ安全弁を掛ける。
    max_content_types: int = Field(ge=MIN_SUMMARY_LIMIT)
    max_difficulties: int = Field(ge=MIN_SUMMARY_LIMIT)


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
    feedback_weights: FeedbackWeightsConfig
    interest_decay: InterestDecayConfig
    confidence: ConfidenceConfig
    topic_preference: TopicPreferenceConfig
    source_preference: SourcePreferenceConfig
    bad_similarity: BadSimilarityConfig
    clustering: ClusteringConfig
    interest_timeline: InterestTimelineConfig
    interest_summary: InterestSummaryConfig

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
            novelty=NoveltySettings(
                default_when_no_embedding=self.novelty.default_when_no_embedding
            ),
            feed_composition=FeedComposition(
                strong_interest=self.feed_composition.strong_interest,
                primary_source=self.feed_composition.primary_source,
                exploration=self.feed_composition.exploration,
                diversity=self.feed_composition.diversity,
                strong_interest_min_similarity=self.feed_composition.strong_interest_min_similarity,
                exploration_min_novelty=self.feed_composition.exploration_min_novelty,
            ),
            limits=RankingLimits(
                max_candidates_per_run=self.limits.max_candidates_per_run,
                default_page_size=self.limits.default_page_size,
                max_page_size=self.limits.max_page_size,
            ),
            bad_similarity=BadSimilaritySettings(
                min_similarity=self.bad_similarity.min_similarity,
                max_penalty=self.bad_similarity.max_penalty,
            ),
            source_preference=SourcePreferenceGate(
                weight_scale=self.source_preference.weight_scale,
                min_factor=self.source_preference.min_factor,
                max_factor=self.source_preference.max_factor,
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
