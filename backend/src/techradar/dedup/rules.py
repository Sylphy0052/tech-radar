"""記事の重複判定（`PROJECT_SPEC.md` §17）。

同一ニュースが複数の情報源から取得された際に、どれを代表として残し、どれを
LLM による独自価値判定へ回すかを決める。判定は副作用を持たない純粋関数として
実装する（`PROJECT_SPEC.md` §25）。DB モデル（`Article`）には依存させず、
判定に必要な項目だけを持つ `ArticleSignature` を入力にする。
"""

from __future__ import annotations

import itertools
import math
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from techradar.db.enums import ContentType, SourceType
from techradar.fetcher.url import normalize_url
from techradar.sources.rules import is_primary_source

# published_at が無い記事を代表選定で常に最後へ回すための番兵値。
_NO_PUBLISHED_AT_SORT_KEY = float("inf")

# 浮動小数点の丸め誤差を吸収する許容差。例えば設定値 0.90 と 0.60 の差は
# 0.30000000000000004 になり、閾値ちょうど 0.30 の記事が意図せず弾かれてしまう。
_FLOAT_TOLERANCE = 1e-9

# タイトル比較に使う文字数の上限。`articles.title` は外部フィード由来の Text 列で
# 長さ制限が無く、編集距離（`_levenshtein_distance`）は O(n*m) のため、極端に長い
# タイトルが混ざると 1 回の実行コストが跳ね上がる。`judge.py` の
# MAX_JUDGE_TITLE_CHARACTERS と同じ考え方（LLM 入力側は既に上限があるが、
# 判定側には無かった）。
MAX_COMPARISON_TITLE_CHARACTERS = 300


@dataclass(frozen=True)
class ArticleSignature:
    """重複判定に使う記事の情報。

    `Article` モデルそのものを渡すと判定ロジックが DB スキーマへ結合してしまう
    ため、判定に必要な項目だけを抜き出した専用の型にする。
    """

    id: uuid.UUID
    canonical_url: str
    original_url: str
    title: str
    body_hash: str | None
    embedding: tuple[float, ...] | None
    source_authority: float
    source_type: SourceType
    content_type: ContentType | None
    technical_quality: float
    published_at: datetime | None


class MatchMethod(StrEnum):
    """どの段で重複と判定したかを表す。"""

    CANONICAL_URL = "canonical_url"
    NORMALIZED_URL = "normalized_url"
    BODY_HASH = "body_hash"
    TITLE = "title"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class DuplicateMatch:
    """2 記事間の重複判定結果。"""

    method: MatchMethod
    similarity: float


@dataclass(frozen=True)
class ArticleCluster:
    """重複と判定された記事の集合。単独記事も 1 件のクラスタとして表す。"""

    members: tuple[ArticleSignature, ...]
    # どのペアがどの段で一致したかの履歴。ログや説明表示に使う想定。
    matches: tuple[tuple[uuid.UUID, uuid.UUID, DuplicateMatch], ...]


@dataclass(frozen=True)
class DuplicateThresholds:
    """重複と判定する類似度の閾値。"""

    title_similarity: float
    embedding_similarity: float


@dataclass(frozen=True)
class DuplicatePenalties:
    """一致した段に応じた減点。完全一致に近い段ほど大きく減点する。"""

    canonical_url: float
    normalized_url: float
    body_hash: float
    title: float
    embedding: float


@dataclass(frozen=True)
class UniqueValueSettings:
    """独自価値判定に回す候補を絞る設定（コスト管理、`PROJECT_SPEC.md` §24）。"""

    content_types: tuple[ContentType, ...]
    min_technical_quality: float
    max_authority_gap: float
    max_candidates_per_cluster: int


@dataclass(frozen=True)
class DedupLimits:
    """1 回の実行の処理量に掛ける安全弁（コスト管理、`PROJECT_SPEC.md` §24）。

    収集元が短期間に大量の記事を入れても、実行コストが際限なく増えないように
    する。値は運用しながら調整するため `config/dedup.yaml` に置く。
    """

    # 1 回の実行で処理する記事数の上限。`cluster_articles` が O(n^2) のため、
    # 超過分は決定的な順序で切り捨てる。
    max_articles_per_run: int
    # 1 回の実行で LLM（独自価値判定）を呼ぶ総数の上限。到達した以降の候補は
    # 判定せず安全側（重複）として扱う。
    max_llm_calls_per_run: int


def normalize_title(title: str) -> str:
    """タイトルの表記ゆれを吸収する。

    Unicode NFKC 正規化で全角・半角の違いを消し、大文字・小文字と記号・空白の
    有無で判定が変わらないよう英数字・かな漢字だけを残す。

    正規化の前に `MAX_COMPARISON_TITLE_CHARACTERS` で切り詰める。編集距離の
    計算コストを抑えるための上限であり、独自価値判定と同じ発想で「冒頭で
    主題は読み取れる」という前提に立つ。
    """
    truncated = title[:MAX_COMPARISON_TITLE_CHARACTERS]
    normalized = unicodedata.normalize("NFKC", truncated).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _levenshtein_distance(left: str, right: str) -> int:
    """編集距離を求める。

    行列全体を持つと記事タイトル程度の長さでもメモリを無駄に使うため、
    直前の行だけを保持する 2 行の動的計画法にする。
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous_row = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current_row = [i]
        for j, right_char in enumerate(right, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (0 if left_char == right_char else 1)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]


def title_similarity(left: str, right: str) -> float:
    """正規化後のタイトルの類似度を 0.0〜1.0 で返す。

    編集距離ベース（`1 - distance / max(len)`）にすることで、外部ライブラリを
    増やさず「【翻訳】」のような接頭辞付与や語順の軽微な違いを吸収できる。
    """
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    if not normalized_left or not normalized_right:
        # 無題（正規化後に空）の記事同士を重複と判定しないための特別扱い。
        return 0.0
    if normalized_left == normalized_right:
        return 1.0

    distance = _levenshtein_distance(normalized_left, normalized_right)
    max_len = max(len(normalized_left), len(normalized_right))
    return 1 - distance / max_len


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """コサイン類似度を返す。

    次元が違う Embedding はモデルの更新などで混入した比較不能な組み合わせ
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


def _normalized_urls(signature: ArticleSignature) -> frozenset[str]:
    """記事が持つ URL の正規形の集合。

    canonical_url と original_url の両方を見る。canonical 抽出に失敗した記事
    や、canonical が無く original だけが登録された記事でも拾えるようにする。
    空文字は「URL が無い」ことを表すだけなので集合から除く。含めたままだと
    canonical 抽出に失敗した無関係な記事同士が、空文字同士の一致で重複判定
    されてしまう。
    """
    return frozenset(
        url
        for url in (normalize_url(signature.canonical_url), normalize_url(signature.original_url))
        if url
    )


def find_duplicate_match(
    left: ArticleSignature, right: ArticleSignature, thresholds: DuplicateThresholds
) -> DuplicateMatch | None:
    """2 記事が重複かを多段で判定する。

    上から順に評価し、最初に一致した段の結果を返す。より確実な根拠（URL の
    完全一致）を優先し、確度の低い根拠（Embedding 類似度）は最後に回す。
    """
    if left.id == right.id:
        return None

    # canonical_url は NOT NULL だが空文字は入りうる（抽出失敗時など）。
    # 空文字同士を一致とみなすと、無関係な記事同士が全て重複扱いになる。
    if left.canonical_url and left.canonical_url == right.canonical_url:
        return DuplicateMatch(method=MatchMethod.CANONICAL_URL, similarity=1.0)

    if _normalized_urls(left) & _normalized_urls(right):
        # トラッキングパラメータ違いなど、正規化すれば同一になる URL を拾う。
        return DuplicateMatch(method=MatchMethod.NORMALIZED_URL, similarity=1.0)

    if left.body_hash is not None and left.body_hash == right.body_hash:
        return DuplicateMatch(method=MatchMethod.BODY_HASH, similarity=1.0)

    title_score = title_similarity(left.title, right.title)
    if title_score >= thresholds.title_similarity - _FLOAT_TOLERANCE:
        return DuplicateMatch(method=MatchMethod.TITLE, similarity=title_score)

    if left.embedding is not None and right.embedding is not None:
        embedding_score = cosine_similarity(left.embedding, right.embedding)
        if embedding_score >= thresholds.embedding_similarity - _FLOAT_TOLERANCE:
            return DuplicateMatch(method=MatchMethod.EMBEDDING, similarity=embedding_score)

    return None


def cluster_articles(
    signatures: Sequence[ArticleSignature], thresholds: DuplicateThresholds
) -> tuple[ArticleCluster, ...]:
    """総当たりで重複関係を求め、推移的に連結したものを 1 クラスタにする。

    「A と B が重複」「B と C が重複」なら A・B・C を 1 クラスタにまとめたい
    （転載が転載を生むケースがあるため）。union-find で推移閉包を取る。
    """
    parent: dict[uuid.UUID, uuid.UUID] = {signature.id: signature.id for signature in signatures}

    def find_root(node: uuid.UUID) -> uuid.UUID:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    pair_matches: list[tuple[uuid.UUID, uuid.UUID, DuplicateMatch]] = []
    for left, right in itertools.combinations(signatures, 2):
        match = find_duplicate_match(left, right, thresholds)
        if match is None:
            continue
        pair_matches.append((left.id, right.id, match))
        root_left, root_right = find_root(left.id), find_root(right.id)
        if root_left != root_right:
            parent[root_right] = root_left

    members_by_root: dict[uuid.UUID, list[ArticleSignature]] = defaultdict(list)
    for signature in signatures:
        members_by_root[find_root(signature.id)].append(signature)

    matches_by_root: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID, DuplicateMatch]]] = (
        defaultdict(list)
    )
    for left_id, right_id, match in pair_matches:
        matches_by_root[find_root(left_id)].append((left_id, right_id, match))

    return tuple(
        ArticleCluster(members=tuple(members), matches=tuple(matches_by_root[root]))
        for root, members in members_by_root.items()
    )


def _published_at_sort_key(published_at: datetime | None) -> float:
    """代表選定で使う published_at の並び替えキー。

    tz-aware / naive が混在しても比較できるよう timestamp に変換する。
    無ければ最後に回るよう無限大にする。
    """
    if published_at is None:
        return _NO_PUBLISHED_AT_SORT_KEY
    return published_at.timestamp()


def _representative_sort_key(
    signature: ArticleSignature,
) -> tuple[float, int, float, str]:
    """代表選定の優先順位。小さいほど優先する（`min` で使う想定）。"""
    return (
        -signature.source_authority,
        0 if is_primary_source(signature.source_type) else 1,
        # 原典が先に出る前提のため、published_at が古い方を優先する。
        _published_at_sort_key(signature.published_at),
        str(signature.id),
    )


def select_representative(cluster: ArticleCluster) -> ArticleSignature:
    """クラスタの代表記事を選ぶ。

    `source_authority` が最大のものを採る。同点なら (a) 一次情報を優先
    (b) published_at が古い方を優先 (c) id の文字列順、で決定的に決める。
    """
    return min(cluster.members, key=_representative_sort_key)


_PENALTY_BY_METHOD: dict[MatchMethod, str] = {
    MatchMethod.CANONICAL_URL: "canonical_url",
    MatchMethod.NORMALIZED_URL: "normalized_url",
    MatchMethod.BODY_HASH: "body_hash",
    MatchMethod.TITLE: "title",
    MatchMethod.EMBEDDING: "embedding",
}


def duplicate_penalty_for(match: DuplicateMatch, penalties: DuplicatePenalties) -> float:
    """一致した段に応じた減点を返す。

    完全一致に近い段（canonical/正規化 URL・body_hash）は満額減点し、確度が
    落ちるタイトル・Embedding 一致は誤判定の影響を抑えるため弱める。
    """
    return getattr(penalties, _PENALTY_BY_METHOD[match.method])


def unique_value_candidates(
    cluster: ArticleCluster,
    representative: ArticleSignature,
    settings: UniqueValueSettings,
) -> tuple[ArticleSignature, ...]:
    """LLM へ独自価値を問う候補だけを絞る（コスト管理、`PROJECT_SPEC.md` §24）。

    クラスタ全件を LLM に投げるとコストが線形に増える。代表以外で
    「解説・実装記事らしく」「一定の技術品質があり」「代表と authority が
    近い」ものだけを、技術品質の高い順に上限件数まで残す。

    ここでの絞り込み条件（content_type 限定・technical_quality 下限・
    authority 差の上限）は `PROJECT_SPEC.md` §17 が定めたものではなく、§24 の
    コスト管理のために本実装が独自に置いたトレードオフである。上流の
    content_type 分類が誤っている記事や、authority の低い情報源にある本物の
    独自検証は LLM に問われないまま重複として畳まれうる。値は
    `config/dedup.yaml` で調整できる。
    """
    candidates = [
        member
        for member in cluster.members
        if member.id != representative.id
        and member.content_type in settings.content_types
        and member.technical_quality >= settings.min_technical_quality - _FLOAT_TOLERANCE
        and (representative.source_authority - member.source_authority)
        <= settings.max_authority_gap + _FLOAT_TOLERANCE
    ]
    # 同点の候補が上限を跨ぐと、二次キーが無い安定ソートでは結果が入力順
    # （SQL が保証しない行順）に依存してしまう。id の文字列順を決定的な
    # 二次キーにし、どの候補が LLM 判定に回るかを実行ごとに変えない
    # （`_representative_sort_key` と同じ考え方）。
    ordered = sorted(candidates, key=lambda member: (-member.technical_quality, str(member.id)))
    return tuple(ordered[: settings.max_candidates_per_cluster])
