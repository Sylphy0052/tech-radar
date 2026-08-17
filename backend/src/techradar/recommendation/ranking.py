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
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

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
    # この user がこの情報源に対して持つ選好（`user_source_preferences.effective_weight`、
    # `interest/sources.py`）。正なら Good を重ねた情報源、負なら Bad が繰り返された
    # 情報源。選好が無い（学習前の）情報源は 0.0（中立）で、既定値もそれに揃える。
    source_preference: float = 0.0


@dataclass(frozen=True)
class WeightedEmbedding:
    """重み付きの関心記事 embedding。

    `weight` は `interest/weights.py` の `compute_effective_interest` が返す
    `effective_interest`（explicit_weight × feedback_weight × recency_decay ×
    confidence、`PROJECT_SPEC.md` §8）。`compute_interest_similarity` の
    加重平均に使う。
    """

    vector: tuple[float, ...]
    weight: float


@dataclass(frozen=True)
class InterestProfile:
    """ユーザーの関心プロファイル（`PROJECT_SPEC.md` §8）。

    関心記事の重み付き embedding 群（`embeddings`）と、Bad 済み記事の
    embedding 群（`bad_embeddings`）を持つ。単一の平均 embedding ではなく
    複数の embedding を保持し、上位 k 件の加重平均で類似度を求める
    （`compute_interest_similarity`）。

    `bad_embeddings` は Bad 近傍抑制（`compute_bad_similarity_penalty`）専用
    で、関心一致度の計算には使わない。Bad は「関心の逆」ではなく「意味的に
    近い記事を抑制する」対象のため重みを持たせない（`PROJECT_SPEC.md` §7.2）。

    `cluster_centroids` は関心クラスタ（`interest/clusters.py` の
    `InterestCluster.centroid`）の重心群で、新規性（`compute_novelty`）が
    「既知の関心クラスタからどれだけ離れているか」を測るのに使う（Issue #89）。
    `embeddings` と違って重みを持たない（クラスタの大小に関わらず、候補が
    どのクラスタからも離れていれば新規とみなすため）。既定値は空タプルで、
    クラスタが構築されていない・対象外のプロファイル（記事起点推薦など）では
    `compute_novelty` が `default_when_no_embedding` にフォールバックする。
    """

    embeddings: tuple[WeightedEmbedding, ...]
    bad_embeddings: tuple[tuple[float, ...], ...]
    cluster_centroids: tuple[tuple[float, ...], ...] = ()


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

    # Bad 済みの候補への減点。主経路は `recommendation/service.py` の
    # `load_candidates` が候補取得の時点で Bad 済み記事そのものを除外することで、
    # `CandidateSignature.is_bad` は本番では常に False（＝この減点は通常到達しない）。
    # ここでの減点は、それをすり抜けた場合に備える純粋関数側の防御的な多重防御。
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
class SourcePreferenceGate:
    """情報源選好を `source_authority` の寄与へ反映する設定（Issue #34）。

    `PROJECT_SPEC.md` §14 の式は `source_authority` をユーザー横断で静的な項と
    して持つ。ユーザー固有の学習結果（`user_source_preferences`、
    `interest/sources.py`）は新しい重み項として足すのではなく、この項に掛ける
    係数として合成する。`AuthorityGate` と同じ乗算合成にすることで、重みの合計を
    1.0 に保つ既存の制約（`recommendation/config.py` の
    `_validate_weights_sum_to_one`）と既存の重み配分をそのまま維持できるため。
    """

    # `effective_weight` を係数へ変換するときの傾き。選好 1.0 あたりこの分だけ
    # 係数が 1.0 から離れる。
    weight_scale: float
    # 係数の下限。Bad が続いた情報源でも権威性の寄与を完全にゼロにはしない。
    min_factor: float
    # 係数の上限。`positive_weight` は累積し続けるため、寄与が青天井にならないよう頭打ちにする。
    max_factor: float


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

    # 候補に embedding が無い、または関心プロファイルが空で比較できないときの既定値。
    default_when_no_embedding: float


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
class BadSimilaritySettings:
    """Bad 記事と意味的に近い記事を抑制する設定（`PROJECT_SPEC.md` §7.2）。

    採点の純粋関数（`score_candidate`）は本 Issue（#15 段階 1）では未対応で、
    Bad 記事との類似度に応じた減点の適用は次段階で行う。この dataclass は
    `ScoringSettings` へ先行して組み込み、`config.py` 側の変換だけを先に揃える。
    """

    # Bad 記事とのコサイン類似度がこれ以上なら抑制対象にする閾値。
    min_similarity: float
    # 類似度 1.0（Bad 記事とほぼ同一）のときの最大減点。
    max_penalty: float


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
    bad_similarity: BadSimilaritySettings
    source_preference: SourcePreferenceGate


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
    # `source_authority` の寄与に掛けた情報源選好の係数（`SourcePreferenceGate`）。
    # `authority_gate_factor` とは独立に掛かる（両方の係数が同時に効きうる）。
    source_preference_factor: float
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
    # Bad 済み記事と意味的に近い候補への減点（`compute_bad_similarity_penalty`）。
    # `bad_penalty`（候補自身が Bad 済みかどうかの固定減点）とは別物。
    bad_similarity_penalty: float
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
            "source_preference_factor": self.source_preference_factor,
            "interest_similarity_contribution": self.interest_similarity_contribution,
            "source_authority_contribution": self.source_authority_contribution,
            "source_article_match_contribution": self.source_article_match_contribution,
            "freshness_contribution": self.freshness_contribution,
            "technical_quality_contribution": self.technical_quality_contribution,
            "novelty_contribution": self.novelty_contribution,
            "bad_penalty": self.bad_penalty,
            "duplicate_penalty": self.duplicate_penalty,
            "read_penalty": self.read_penalty,
            "bad_similarity_penalty": self.bad_similarity_penalty,
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

    関心記事群の embedding それぞれとのコサイン類似度のうち、上位 `top_k` 件を
    `WeightedEmbedding.weight` で加重平均する（`PROJECT_SPEC.md` §8）。上位
    `top_k` 件の選び方自体は従来どおり類似度の降順（weight では選ばない）。
    同順位の並びは安定ソートにより入力 `profile.embeddings` の並び順で決まる
    ため、実行のたびに変わらない。

    weight の合計が 0（正負の weight が打ち消し合った場合など）だと加重平均が
    0 除算になるため、その場合は単純平均へフォールバックする。全ての weight
    が等しいときは常にこの単純平均と一致する（退行テストで固定）。

    候補に embedding が無い、または関心プロファイルが空なら比較できないため
    0.0 とする。
    """
    if candidate.embedding is None or not profile.embeddings:
        return 0.0

    scored = sorted(
        (
            (cosine_similarity(candidate.embedding, item.vector), item.weight)
            for item in profile.embeddings
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    top = scored[: settings.top_k]
    weight_sum = sum(weight for _, weight in top)
    if weight_sum == 0.0:
        similarities = [similarity for similarity, _ in top]
        return sum(similarities) / len(similarities)
    return sum(similarity * weight for similarity, weight in top) / weight_sum


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
    candidate: CandidateSignature,
    profile: InterestProfile,
    settings: NoveltySettings,
    *,
    neighbor_similarity: float | None,
) -> float:
    """新規性のスコアを返す（Issue #89）。

    Issue #87 時点の実装は、候補の embedding と関心記事群（`profile.embeddings`）
    とのコサイン類似度の最大値を裏返した値だった。これは
    `compute_interest_similarity`（上位 `top_k` の加重平均）とほぼ完全な相補
    になっており（実データでの Spearman 順位相関 -0.991）、`composition.py` の
    `_slot_for` が 1 段目（strong_interest）と 3 段目（exploration）で同じ値の
    表裏しか見られず、枠判定が実質 1 軸になっていた（Issue #89 の核心）。

    実データ（関心記事 69 件 / 候補 163 件 / クラスタ 8 個）で 6 つの式を比較し、
    次の 2 成分の min を採用した（Spearman -0.687、分布 min 0.094 / p25 0.320 /
    p50 0.429 / p75 0.501 / max 0.738）。

    * `cluster_part`: 候補と `profile.cluster_centroids`（関心クラスタの重心群、
      `interest/clusters.py`）とのコサイン類似度の最大値を `1 -` した値。
      「既知の関心クラスタ群からどれだけ離れているか」を測る
    * `neighbor_part`: 候補と `neighbor_similarity`（同じ採点対象の他の候補との
      コサイン類似度の最大値、`rank_candidates` が一括計算して渡す）を
      `1 -` した値。「今回の候補集合の中でどれだけ孤立しているか」を測る

    min を取るのは、どちらか一方だけが高くても新規とは言えないため
    （既知クラスタから離れていても、今回の候補集合の中に似た記事が大量に
      あれば「新規テーマ」というより「今回たまたま束で来ただけ」であり、
      逆に集合内で孤立していても既知クラスタのど真ん中なら新規ではない）。

    `neighbor_similarity` が `None`（比較対象になる他の候補が無い。採点対象が
    候補 1 件だけ、または他が全て embedding 無しか次元不一致）のときは
    `neighbor_part` を測れないため min の対象から外し、`cluster_part` を
    そのまま返す。`default_when_no_embedding`（中立値）との min は取らない。
    取ってしまうと、候補が 1 件だけのときに限って novelty が不自然に
    切り下がる（他の成分と扱いが非対称になる）ため。

    候補に embedding が無い、または `profile.cluster_centroids` が空（クラスタ
    未構築・記事起点推薦など）なら `cluster_part` 自体を測れないため
    `default_when_no_embedding` を返す。コサイン類似度は負にもなりうるため、
    各成分は `_clamp01` で [0.0, 1.0] へ丸める。
    """
    if candidate.embedding is None or not profile.cluster_centroids:
        return settings.default_when_no_embedding

    max_centroid_similarity = max(
        cosine_similarity(candidate.embedding, centroid) for centroid in profile.cluster_centroids
    )
    cluster_part = _clamp01(1.0 - max_centroid_similarity)

    if neighbor_similarity is None:
        return cluster_part

    neighbor_part = _clamp01(1.0 - neighbor_similarity)
    return min(cluster_part, neighbor_part)


def _compute_neighbor_similarities(
    candidates: Sequence[CandidateSignature],
) -> tuple[float | None, ...]:
    """候補群それぞれについて、同じ採点対象の他の候補との最大コサイン類似度を返す。

    `compute_novelty` の `neighbor_part`（Issue #89）の入力。純 Python の総当たり
    （候補数の 2 乗）は候補 500 件（`limits.max_candidates_per_run` の上限）で
    20.41 秒かかると実測されており、numpy の行列積に置き換えると 0.14 秒になる
    （`docs/adr/0007-embedding-based-novelty.md` の実測表）。そのため素朴な
    二重ループにはせず、embedding を正規化した行列同士の積で一括計算する。

    embedding が無い候補は比較できないため `None`。次元が違う embedding が
    候補間に混ざる可能性があるため（モデル更新などによる混入、
    `cosine_similarity` が次元不一致を 0.0 として扱っているのと同じ理由）、
    最も多くの候補が共有する次元（`dominant_dim`）のグループだけを行列計算の
    対象にし、それ以外の次元の候補は比較対象が無いものとして `None` のままにする
    （少数の次元違いのために全体を Python ループへ落とさないため）。

    比較対象になる他の候補が 1 件も無い（採点対象が自分だけ、または
    dominant_dim のグループに自分しかいない）候補も `None`。
    """
    result: list[float | None] = [None] * len(candidates)

    indices_by_dim: dict[int, list[int]] = defaultdict(list)
    for index, candidate in enumerate(candidates):
        if candidate.embedding is not None:
            indices_by_dim[len(candidate.embedding)].append(index)

    if not indices_by_dim:
        return tuple(result)

    dominant_dim = max(indices_by_dim, key=lambda dim: len(indices_by_dim[dim]))
    indices = indices_by_dim[dominant_dim]
    if len(indices) < 2:
        return tuple(result)

    matrix = np.array([candidates[index].embedding for index in indices], dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1)
    # ゼロベクトルは正規化できないため 1.0 で代用する。分子（内積）もゼロのままなので
    # 結果の類似度は 0.0 になり、`cosine_similarity` がゼロベクトルを 0.0 として
    # 扱うのと同じ挙動になる（0 除算を避けるためだけの epsilon ではない）。
    safe_norms = np.where(norms == 0.0, 1.0, norms)
    normalized = matrix / safe_norms[:, np.newaxis]
    similarity_matrix = normalized @ normalized.T
    # 対角成分（自分自身との類似度 1.0）を最大値の対象から除外する。
    np.fill_diagonal(similarity_matrix, -np.inf)
    max_similarities = similarity_matrix.max(axis=1)

    for position, original_index in enumerate(indices):
        result[original_index] = float(max_similarities[position])

    return tuple(result)


def compute_bad_similarity_penalty(
    candidate: CandidateSignature, profile: InterestProfile, settings: BadSimilaritySettings
) -> float:
    """Bad 済み記事と意味的に近い候補への減点を返す（`PROJECT_SPEC.md` §7.2）。

    候補の embedding と `profile.bad_embeddings` それぞれとのコサイン類似度の
    うち最大値 s を求める。「1 件の Bad でジャンル全体を抑制しない」ため、s が
    `min_similarity` 未満なら近さが十分ではないとみなし 0.0。s が
    `min_similarity` 以上なら、`min_similarity` で 0、s=1.0（Bad 記事とほぼ
    同一）で `max_penalty` になるよう線形に減点する。

    候補に embedding が無い、`bad_embeddings` が空なら比較できないため 0.0。

    既存の `ScorePenalties.bad`（候補自身が Bad 済みかどうかの二値による固定
    減点）とは別物。こちらは候補自身は Bad ではなくても、Bad にした「別の」
    記事と意味的に近い場合に働く抑制であり、両者は独立して減点されうる。
    """
    if candidate.embedding is None or not profile.bad_embeddings:
        return 0.0

    max_similarity = max(
        cosine_similarity(candidate.embedding, bad_embedding)
        for bad_embedding in profile.bad_embeddings
    )
    if max_similarity < settings.min_similarity:
        return 0.0

    span = _RATIO_MAX - settings.min_similarity
    if span <= 0.0:
        return settings.max_penalty

    ratio = _clamp01((max_similarity - settings.min_similarity) / span)
    return settings.max_penalty * ratio


def compute_source_preference_factor(source_preference: float, gate: SourcePreferenceGate) -> float:
    """情報源選好を `source_authority` の寄与に掛ける係数へ変換する（Issue #34）。

    `source_preference` は `user_source_preferences.effective_weight`
    （`interest/sources.py` の `compute_effective_weight` が返す
    `positive - negative` の符号付きの値）。選好が無い情報源（0.0）では 1.0 を
    返し、従来のスコアと一致する（学習前は挙動を変えない）。

    係数は `1.0 + weight_scale × source_preference` を `[min_factor, max_factor]`
    で挟んだ値。`positive_weight` は Good のたびに累積し続けるため上限が無いと
    寄与が青天井になり、逆に Bad が続いた情報源も下限が無いと権威性の寄与が
    ゼロまで落ちてしまう。「抑制はするが完全排除はしない」という §6.1 の既読
    減点と同じ設計思想に揃えるため、両側で頭打ちにする。
    """
    raw = 1.0 + gate.weight_scale * source_preference
    return min(max(raw, gate.min_factor), gate.max_factor)


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
    *,
    neighbor_similarity: float | None,
) -> ScoreBreakdown:
    """1 件の候補のスコア内訳を返す（`PROJECT_SPEC.md` §14）。

    `neighbor_similarity` は `compute_novelty` の `neighbor_part`（Issue #89）の
    入力で、同じ採点対象の他の候補との最大コサイン類似度（無ければ `None`）。
    既定値を付けていないのは意図的で、呼び出し側（`rank_candidates`）が
    `_compute_neighbor_similarities` の一括計算結果を渡し忘れたまま
    `settings.novelty.default_when_no_embedding` にフォールバックし続ける事故を
    型で気付けるようにするため。
    """
    interest_similarity = compute_interest_similarity(profile, candidate, settings.interest)
    source_authority = _clamp01(candidate.source_authority)
    source_article_match = compute_source_article_match(candidate, settings.source_match)
    freshness = compute_freshness(candidate, now, settings.freshness)
    technical_quality = _clamp01(candidate.technical_quality)
    novelty = compute_novelty(
        candidate, profile, settings.novelty, neighbor_similarity=neighbor_similarity
    )

    gate_factor = _authority_gate_factor(interest_similarity, settings.authority_gate)
    # ユーザー固有の情報源選好（Issue #34）。`gate_factor` とは独立に、同じ
    # `source_authority` の寄与へ掛け合わせる。
    preference_factor = compute_source_preference_factor(
        candidate.source_preference, settings.source_preference
    )

    weights = settings.weights
    interest_similarity_contribution = interest_similarity * weights.interest_similarity
    source_authority_contribution = (
        source_authority * weights.source_authority * gate_factor * preference_factor
    )
    source_article_match_contribution = source_article_match * weights.source_article_match
    freshness_contribution = freshness * weights.freshness
    technical_quality_contribution = technical_quality * weights.technical_quality
    novelty_contribution = novelty * weights.novelty

    # is_bad は主経路（load_candidates の除外）では常に False。ここでの減点は
    # 防御的な多重防御であり、本番では通常発火しない（ScorePenalties.bad 参照）。
    bad_penalty = settings.penalties.bad if candidate.is_bad else 0.0
    duplicate_penalty = candidate.duplicate_penalty
    read_penalty = settings.penalties.read if candidate.is_read else 0.0
    bad_similarity_penalty = compute_bad_similarity_penalty(
        candidate, profile, settings.bad_similarity
    )

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
        - bad_similarity_penalty
    )

    return ScoreBreakdown(
        interest_similarity=interest_similarity,
        source_authority=source_authority,
        source_article_match=source_article_match,
        freshness=freshness,
        technical_quality=technical_quality,
        novelty=novelty,
        authority_gate_factor=gate_factor,
        source_preference_factor=preference_factor,
        interest_similarity_contribution=interest_similarity_contribution,
        source_authority_contribution=source_authority_contribution,
        source_article_match_contribution=source_article_match_contribution,
        freshness_contribution=freshness_contribution,
        technical_quality_contribution=technical_quality_contribution,
        novelty_contribution=novelty_contribution,
        bad_penalty=bad_penalty,
        duplicate_penalty=duplicate_penalty,
        read_penalty=read_penalty,
        bad_similarity_penalty=bad_similarity_penalty,
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

    採点前に `_compute_neighbor_similarities` で全候補の最近傍類似度を一括計算
    してから `score_candidate` へ渡す（Issue #89）。候補ごとに他の全候補との
    類似度を測る必要があり、`score_candidate` の外（候補集合全体を見られる
    ここ）でしか計算できない。
    """
    neighbor_similarities = _compute_neighbor_similarities(candidates)
    scored = (
        ScoredCandidate(
            candidate=candidate,
            breakdown=score_candidate(
                candidate,
                profile,
                settings,
                now,
                neighbor_similarity=neighbor_similarities[index],
            ),
        )
        for index, candidate in enumerate(candidates)
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
