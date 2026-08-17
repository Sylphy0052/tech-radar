"""切り捨てが解析結果へ与える影響の比較（Issue #73）。

`MAX_ANALYSIS_BODY_CHARACTERS` を確定するには、応答時間だけでなく品質側の影響も
知る必要がある。同じ記事について「切り捨てた本文」と「全文」で解析した 2 つの
`ArticleAnalysis` を受け取り、出力がどれだけ変わるかを比較する。

比較する観点はフィールドの性質で分ける。

- 完全一致で見るフィールド（`domain` / `category` / `content_type` / `difficulty`）:
  分類が変わったかどうかがそのまま結果になる。
- 集合の重なりで見るフィールド（`topics` / `technologies`）: LLM の出力は順序も
  個数も安定しないため、一致数ではなく Jaccard 係数（積集合 / 和集合）で測る。
  両方空なら要素の点で違いが無いとみなし 1.0 とする。
- 数値の差で見るフィールド（`technical_quality`）: 絶対差を見る。
- `translated_title` / `summary_ja` は自動では優劣を判定できない。一致したかどうかと
  両方のテキストを保持するに留め、判断は人に委ねる。

LLM 呼び出しはここでは行わない。純粋関数として書き、テストで値を固定できるようにする。
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from techradar.analysis.schema import ArticleAnalysis


@dataclass(frozen=True)
class TruncationImpact:
    """1 記事分の「切り捨て版」と「全文版」の比較結果。"""

    domain_matches: bool
    category_matches: bool
    content_type_matches: bool
    difficulty_matches: bool
    topics_jaccard: float
    technologies_jaccard: float
    technical_quality_diff: float
    translated_title_matches: bool
    truncated_translated_title: str | None
    full_translated_title: str | None
    summary_matches: bool
    truncated_summary_ja: str
    full_summary_ja: str


@dataclass(frozen=True)
class TruncationImpactSummary:
    """複数記事分の比較結果の集計。

    `compared_count` は比較できた件数（LLM 呼び出しに失敗した記事は含まない）。
    比較できた記事が 0 件のときは、率・中央値はすべて `None` にする。
    """

    compared_count: int
    failed_count: int
    domain_match_rate: float | None
    category_match_rate: float | None
    content_type_match_rate: float | None
    difficulty_match_rate: float | None
    topics_jaccard_median: float | None
    technologies_jaccard_median: float | None
    technical_quality_diff_median: float | None


def _normalize_labels(values: Sequence[str]) -> set[str]:
    """表記ゆれ（大文字小文字・前後の空白）を吸収してから比較する。"""
    return {value.strip().lower() for value in values}


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    """2 つの集合の Jaccard 係数（積集合 / 和集合）を返す。

    両方空なら要素の点で違いが無いとみなし 1.0 とする。片方だけ空なら和集合が
    もう片方の集合そのものになるため、自然に 0.0 になる。
    """
    left_set = _normalize_labels(left)
    right_set = _normalize_labels(right)
    union = left_set | right_set
    if not union:
        return 1.0
    return len(left_set & right_set) / len(union)


def compare_analyses(truncated: ArticleAnalysis, full: ArticleAnalysis) -> TruncationImpact:
    """切り捨て版と全文版の解析結果を比較する。"""
    return TruncationImpact(
        domain_matches=truncated.domain == full.domain,
        category_matches=truncated.category == full.category,
        content_type_matches=truncated.content_type == full.content_type,
        difficulty_matches=truncated.difficulty == full.difficulty,
        topics_jaccard=_jaccard(truncated.topics, full.topics),
        technologies_jaccard=_jaccard(truncated.technologies, full.technologies),
        technical_quality_diff=abs(truncated.technical_quality - full.technical_quality),
        translated_title_matches=truncated.translated_title == full.translated_title,
        truncated_translated_title=truncated.translated_title,
        full_translated_title=full.translated_title,
        summary_matches=truncated.summary_ja == full.summary_ja,
        truncated_summary_ja=truncated.summary_ja,
        full_summary_ja=full.summary_ja,
    )


def summarize_truncation_impacts(
    impacts: Sequence[TruncationImpact], *, failed_count: int = 0
) -> TruncationImpactSummary:
    """複数記事分の比較結果をまとめる。

    `failed_count` は呼び出し側（LLM 呼び出しの成否を知っている層）から渡す。
    この関数自体は比較結果の列しか受け取らないため、失敗した記事の件数を
    自分では数えられない。
    """
    if not impacts:
        return TruncationImpactSummary(
            compared_count=0,
            failed_count=failed_count,
            domain_match_rate=None,
            category_match_rate=None,
            content_type_match_rate=None,
            difficulty_match_rate=None,
            topics_jaccard_median=None,
            technologies_jaccard_median=None,
            technical_quality_diff_median=None,
        )

    count = len(impacts)
    return TruncationImpactSummary(
        compared_count=count,
        failed_count=failed_count,
        domain_match_rate=sum(1 for impact in impacts if impact.domain_matches) / count,
        category_match_rate=sum(1 for impact in impacts if impact.category_matches) / count,
        content_type_match_rate=sum(1 for impact in impacts if impact.content_type_matches) / count,
        difficulty_match_rate=sum(1 for impact in impacts if impact.difficulty_matches) / count,
        topics_jaccard_median=statistics.median(impact.topics_jaccard for impact in impacts),
        technologies_jaccard_median=statistics.median(
            impact.technologies_jaccard for impact in impacts
        ),
        technical_quality_diff_median=statistics.median(
            impact.technical_quality_diff for impact in impacts
        ),
    )
