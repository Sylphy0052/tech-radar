"""推薦スコアの計算（`PROJECT_SPEC.md` §14, §15）。

「公式であることだけで上位表示しない」ため、`source_authority` の寄与は
関心一致度（`interest_similarity`）でゲートを掛けて弱める（§14 補足）。判定は
副作用を持たない純粋関数として実装する（`PROJECT_SPEC.md` §25）。DB モデル
（`Article`）には依存させず、採点に必要な項目だけを持つ `CandidateSignature` を
入力にする。
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

# 経過日数を求めるための秒数。
_SECONDS_PER_DAY = 86400

# authority_gate の線形補間・freshness の減衰計算に使う比率を [0.0, 1.0] へ丸めるための境界。
_RATIO_MIN = 0.0
_RATIO_MAX = 1.0

# 理由文の根拠として述べる項目数（`build_reason_summary`、寄与が大きい順）。
_REASON_TOP_N = 2


@dataclass(frozen=True)
class CandidateSignature:
    """採点対象の記事候補。

    `Article` モデルそのものを渡すと採点ロジックが DB スキーマへ結合してしまう
    ため、採点に必要な項目だけを抜き出した専用の型にする。
    """

    id: uuid.UUID
    embedding: tuple[float, ...] | None
    source_authority: float
    is_primary_source: bool
    # フィードの多様性枠（`composition.py`）が、既選択候補とのドメイン重複判定に使う。
    source_domain: str
    source_entity_names: tuple[str, ...]
    topics: tuple[str, ...]
    technologies: tuple[str, ...]
    technical_quality: float
    published_at: datetime | None
    fetched_at: datetime
    duplicate_penalty: float
    is_bad: bool
    is_read: bool


@dataclass(frozen=True)
class InterestProfile:
    """ユーザーの関心プロファイル（`PROJECT_SPEC.md` §8）。

    関心記事の embedding 群と、既知トピックの集合を持つ。単一の平均 embedding
    ではなく複数の embedding を保持し、上位 k 件の平均で類似度を求める
    （`compute_interest_similarity`）。
    """

    embeddings: tuple[tuple[float, ...], ...]
    known_topics: frozenset[str]


@dataclass(frozen=True)
class ScoreWeights:
    """スコア各項目の重み（`PROJECT_SPEC.md` §14）。合計は 1.0 にする。"""

    interest_similarity: float
    source_authority: float
    source_article_match: float
    freshness: float
    technical_quality: float
    novelty: float


@dataclass(frozen=True)
class ScorePenalties:
    """候補の状態に応じた固定減点。"""

    # Bad 済みの候補への減点。フィードからの除外はサービス層の責務で、
    # ここでは減点のみを行う。
    bad: float
    # 既読記事の再表示を抑制するための減点（`PROJECT_SPEC.md` §6.1）。
    read: float


@dataclass(frozen=True)
class AuthorityGate:
    """`source_authority` の寄与を関心一致度でゲートする設定（`PROJECT_SPEC.md` §14 補足）。

    「公式であることだけで上位表示しない」ため、関心一致度が低い候補では
    authority の寄与を弱める。
    """

    min_interest_similarity: float
    min_factor: float


@dataclass(frozen=True)
class FreshnessSettings:
    """新しさによる減衰の設定。"""

    max_age_days: float


@dataclass(frozen=True)
class InterestSettings:
    """関心一致度の計算設定。"""

    # 関心プロファイルとの類似度上位何件を平均するか。
    top_k: int


@dataclass(frozen=True)
class MatchSettings:
    """情報源と記事主題の一致度の計算設定。"""

    # 完全一致ではなく部分一致（エンティティ名がトピック文字列に含まれる、
    # またはその逆）の場合に使うスコア。
    partial_match_score: float


@dataclass(frozen=True)
class NoveltySettings:
    """新規性の計算設定。"""

    # topics が空の記事に使う既定値。
    default_when_no_topics: float


@dataclass(frozen=True)
class FeedComposition:
    """Discover フィードの構成比の目安（`PROJECT_SPEC.md` §15）。

    比率 4 項目（`strong_interest` 〜 `diversity`）の合計は 1.0 にする。
    枠判定用の閾値 2 項目（`composition.py` が使う）は比率合計には含めない。
    """

    strong_interest: float
    primary_source: float
    exploration: float
    diversity: float
    # 関心一致度がこの値以上なら「強い関心一致」枠とみなす（`composition.py`）。
    strong_interest_min_similarity: float
    # 新規性がこの値以上なら「新規テーマ探索」枠とみなす（`composition.py`）。
    exploration_min_novelty: float


@dataclass(frozen=True)
class RankingLimits:
    """ランキング処理の安全弁とページングの既定値。"""

    # 1 回の実行で採点する候補数の上限。
    max_candidates_per_run: int
    # 1 ページあたりの既定件数。
    default_page_size: int
    # 1 ページあたりに許す最大件数。
    max_page_size: int


@dataclass(frozen=True)
class ScoringSettings:
    """`score_candidate` / `rank_candidates` が受け取る設定一式。"""

    weights: ScoreWeights
    penalties: ScorePenalties
    authority_gate: AuthorityGate
    freshness: FreshnessSettings
    interest: InterestSettings
    source_match: MatchSettings
    novelty: NoveltySettings
    feed_composition: FeedComposition
    limits: RankingLimits


# 寄与項目名と、理由文に使う言い回し（`build_reason_summary`）。
# 「寄与が大きい順に上位 2 項目を根拠として述べる」ための言い回しを、
# (連用形, 終止形) の組で持つ。列挙が続く項目は連用形（「〜く」「〜おり」）で
# つなぎ、最後の項目だけ終止形にして「ため」へ自然に接続する。
_REASON_PHRASES_BY_COMPONENT: dict[str, tuple[str, str]] = {
    "interest_similarity": ("関心との一致度が高く", "関心との一致度が高い"),
    "source_authority": ("情報源の権威性が高く", "情報源の権威性が高い"),
    "source_article_match": (
        "情報源と記事の主題が一致しており",
        "情報源と記事の主題が一致している",
    ),
    "freshness": ("公開から日が浅く", "公開から日が浅い"),
    "technical_quality": ("技術的な質が高く", "技術的な質が高い"),
    "novelty": ("新規性が高く", "新規性が高い"),
}


@dataclass(frozen=True)
class ScoreBreakdown:
    """スコアの内訳。`recommendations.reasons`（JSONB）へ格納する情報の元になる。"""

    # 重み適用前の素の値（0.0〜1.0）。
    interest_similarity: float
    source_authority: float
    source_article_match: float
    freshness: float
    technical_quality: float
    novelty: float
    # `source_authority` の寄与に掛けたゲート係数（`AuthorityGate`）。
    authority_gate_factor: float
    # 重み（と authority_gate）適用後の寄与。
    interest_similarity_contribution: float
    source_authority_contribution: float
    source_article_match_contribution: float
    freshness_contribution: float
    technical_quality_contribution: float
    novelty_contribution: float
    # 減点。
    bad_penalty: float
    duplicate_penalty: float
    read_penalty: float
    # 寄与の合計から減点を引いた最終スコア。
    total: float

    def to_reasons(self) -> dict[str, float | str]:
        """`recommendations.reasons`（JSONB）へ格納する内訳を返す。

        キーは snake_case、値は各項目の数値と、機械生成した日本語 1 文
        （`summary`）。
        """
        return {
            "interest_similarity": self.interest_similarity,
            "source_authority": self.source_authority,
            "source_article_match": self.source_article_match,
            "freshness": self.freshness,
            "technical_quality": self.technical_quality,
            "novelty": self.novelty,
            "authority_gate_factor": self.authority_gate_factor,
            "interest_similarity_contribution": self.interest_similarity_contribution,
            "source_authority_contribution": self.source_authority_contribution,
            "source_article_match_contribution": self.source_article_match_contribution,
            "freshness_contribution": self.freshness_contribution,
            "technical_quality_contribution": self.technical_quality_contribution,
            "novelty_contribution": self.novelty_contribution,
            "bad_penalty": self.bad_penalty,
            "duplicate_penalty": self.duplicate_penalty,
            "read_penalty": self.read_penalty,
            "total": self.total,
            "summary": build_reason_summary(self),
        }


@dataclass(frozen=True)
class ScoredCandidate:
    """採点済みの候補。"""

    candidate: CandidateSignature
    breakdown: ScoreBreakdown


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """コサイン類似度を返す。

    次元が違う embedding はモデルの更新などで混入した比較不能な組み合わせ
    なので、例外にはせず 0.0（無関係）として扱う。
    """
    if len(left) != len(right) or not left:
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _clamp01(value: float) -> float:
    """値を [0.0, 1.0] へ丸める。"""
    return min(max(value, _RATIO_MIN), _RATIO_MAX)


def _normalize_text(value: str) -> str:
    """比較用にテキストを正規化する（前後空白除去 + 小文字化）。"""
    return value.strip().lower()


def compute_interest_similarity(
    profile: InterestProfile, candidate: CandidateSignature, settings: InterestSettings
) -> float:
    """関心プロファイルと候補の類似度を返す。

    関心記事群の embedding それぞれとのコサイン類似度のうち、上位 `top_k` 件の
    平均を返す。候補に embedding が無い、または関心プロファイルが空なら
    比較できないため 0.0 とする。
    """
    if candidate.embedding is None or not profile.embeddings:
        return 0.0

    similarities = sorted(
        (cosine_similarity(candidate.embedding, embedding) for embedding in profile.embeddings),
        reverse=True,
    )
    top = similarities[: settings.top_k]
    return sum(top) / len(top)


def compute_source_article_match(candidate: CandidateSignature, settings: MatchSettings) -> float:
    """情報源と記事主題の一致度を返す。

    候補の情報源エンティティ名（`source_entity_names`）が `topics` /
    `technologies` に含まれるかで測る。比較は正規化文字列（前後空白除去・
    小文字化）で行い、完全一致なら 1.0、部分一致（互いに文字列として
    含み合う）なら `partial_match_score`、一致なしは 0.0。複数一致時は最大値。
    """
    if not candidate.source_entity_names:
        return 0.0

    topic_texts = tuple(
        normalized
        for text in (*candidate.topics, *candidate.technologies)
        if (normalized := _normalize_text(text))
    )
    if not topic_texts:
        return 0.0

    best = 0.0
    for entity_name in candidate.source_entity_names:
        normalized_entity = _normalize_text(entity_name)
        if not normalized_entity:
            continue
        for topic_text in topic_texts:
            if normalized_entity == topic_text:
                best = max(best, 1.0)
            elif normalized_entity in topic_text or topic_text in normalized_entity:
                best = max(best, settings.partial_match_score)
    return best


def compute_freshness(
    candidate: CandidateSignature, now: datetime, settings: FreshnessSettings
) -> float:
    """新しさによるスコアを返す。

    `published_at`（無ければ `fetched_at`）から `now` までの経過日数で線形減衰
    する。0 日で 1.0、`max_age_days` 以上で 0.0。未来日付（クロックずれなど）は
    1.0 に丸める。
    """
    reference = candidate.published_at or candidate.fetched_at
    age_days = (now - reference).total_seconds() / _SECONDS_PER_DAY
    if age_days <= 0.0:
        return 1.0
    if age_days >= settings.max_age_days:
        return 0.0
    return 1.0 - age_days / settings.max_age_days


def compute_novelty(
    candidate: CandidateSignature, profile: InterestProfile, settings: NoveltySettings
) -> float:
    """新規性のスコアを返す。

    `topics` のうちユーザーの既知トピック集合（`known_topics`）に無いものの
    割合。`topics` が空なら比較できないため `default_when_no_topics` を返す。
    """
    if not candidate.topics:
        return settings.default_when_no_topics

    known_topics = frozenset(_normalize_text(topic) for topic in profile.known_topics)
    unknown_count = sum(
        1 for topic in candidate.topics if _normalize_text(topic) not in known_topics
    )
    return unknown_count / len(candidate.topics)


def _authority_gate_factor(interest_similarity: float, gate: AuthorityGate) -> float:
    """`source_authority` の寄与に掛けるゲート係数を返す。

    関心一致度が `min_interest_similarity` 以上なら 1.0、0.0 以下なら
    `min_factor`、その間は線形補間する。「公式であることだけで上位表示しない」
    ための補正（`PROJECT_SPEC.md` §14 補足）。
    """
    if interest_similarity >= gate.min_interest_similarity:
        return 1.0
    if gate.min_interest_similarity <= 0.0:
        return 1.0

    ratio = _clamp01(interest_similarity / gate.min_interest_similarity)
    return gate.min_factor + (1.0 - gate.min_factor) * ratio


def score_candidate(
    candidate: CandidateSignature,
    profile: InterestProfile,
    settings: ScoringSettings,
    now: datetime,
) -> ScoreBreakdown:
    """1 件の候補のスコア内訳を返す（`PROJECT_SPEC.md` §14）。"""
    interest_similarity = compute_interest_similarity(profile, candidate, settings.interest)
    source_authority = _clamp01(candidate.source_authority)
    source_article_match = compute_source_article_match(candidate, settings.source_match)
    freshness = compute_freshness(candidate, now, settings.freshness)
    technical_quality = _clamp01(candidate.technical_quality)
    novelty = compute_novelty(candidate, profile, settings.novelty)

    gate_factor = _authority_gate_factor(interest_similarity, settings.authority_gate)

    weights = settings.weights
    interest_similarity_contribution = interest_similarity * weights.interest_similarity
    source_authority_contribution = source_authority * weights.source_authority * gate_factor
    source_article_match_contribution = source_article_match * weights.source_article_match
    freshness_contribution = freshness * weights.freshness
    technical_quality_contribution = technical_quality * weights.technical_quality
    novelty_contribution = novelty * weights.novelty

    bad_penalty = settings.penalties.bad if candidate.is_bad else 0.0
    duplicate_penalty = candidate.duplicate_penalty
    read_penalty = settings.penalties.read if candidate.is_read else 0.0

    total = (
        interest_similarity_contribution
        + source_authority_contribution
        + source_article_match_contribution
        + freshness_contribution
        + technical_quality_contribution
        + novelty_contribution
        - bad_penalty
        - duplicate_penalty
        - read_penalty
    )

    return ScoreBreakdown(
        interest_similarity=interest_similarity,
        source_authority=source_authority,
        source_article_match=source_article_match,
        freshness=freshness,
        technical_quality=technical_quality,
        novelty=novelty,
        authority_gate_factor=gate_factor,
        interest_similarity_contribution=interest_similarity_contribution,
        source_authority_contribution=source_authority_contribution,
        source_article_match_contribution=source_article_match_contribution,
        freshness_contribution=freshness_contribution,
        technical_quality_contribution=technical_quality_contribution,
        novelty_contribution=novelty_contribution,
        bad_penalty=bad_penalty,
        duplicate_penalty=duplicate_penalty,
        read_penalty=read_penalty,
        total=total,
    )


def rank_candidates(
    candidates: Sequence[CandidateSignature],
    profile: InterestProfile,
    settings: ScoringSettings,
    now: datetime,
) -> tuple[ScoredCandidate, ...]:
    """候補群を採点し、スコア降順で並べる。

    同点は id の文字列順にすることで、実行のたびに順序が変わらないようにする
    （`select_representative` と同じ考え方、`dedup/rules.py`）。
    """
    scored = (
        ScoredCandidate(
            candidate=candidate, breakdown=score_candidate(candidate, profile, settings, now)
        )
        for candidate in candidates
    )
    return tuple(
        sorted(
            scored,
            key=lambda scored_candidate: (
                -scored_candidate.breakdown.total,
                str(scored_candidate.candidate.id),
            ),
        )
    )


def build_reason_summary(breakdown: ScoreBreakdown) -> str:
    """スコア内訳から機械生成する日本語 1 文の推薦理由を返す。

    寄与（重み適用後の値）が大きい順に上位 `_REASON_TOP_N` 項目を根拠として
    述べる。LLM は使わない。
    """
    contributions = (
        ("interest_similarity", breakdown.interest_similarity_contribution),
        ("source_authority", breakdown.source_authority_contribution),
        ("source_article_match", breakdown.source_article_match_contribution),
        ("freshness", breakdown.freshness_contribution),
        ("technical_quality", breakdown.technical_quality_contribution),
        ("novelty", breakdown.novelty_contribution),
    )
    top = sorted(contributions, key=lambda item: item[1], reverse=True)[:_REASON_TOP_N]
    top_names = [name for name, _ in top]
    # 列挙が続く項目は連用形、最後の項目だけ終止形にして「ため」へ自然に接続する。
    leading_phrases = [_REASON_PHRASES_BY_COMPONENT[name][0] for name in top_names[:-1]]
    final_phrase = _REASON_PHRASES_BY_COMPONENT[top_names[-1]][1]
    return "、".join([*leading_phrases, final_phrase]) + "ため、上位に表示しています。"
