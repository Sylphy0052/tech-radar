"""切り捨てが解析結果へ与える影響を実測する（Issue #73）。

    cd backend
    uv run python -m techradar.measure.run_truncation_impact
    uv run python -m techradar.measure.run_truncation_impact --articles 5 --json
    uv run python -m techradar.measure.run_truncation_impact --control

`MAX_ANALYSIS_BODY_CHARACTERS`（既定 12000）を超える記事のうち、本文長が短い順に
`--articles` 件選ぶ。本番 DB には全リリースノートを連結したような巨大な外れ値
（実測で 546409 文字）が混ざっており、最長の記事を選ぶと通常の技術記事の代表に
ならない（`run_llm_latency.py` と同じ理由）。

1 記事につき「切り捨て版」（本文を上限で切ったもの）と「全文版」の 2 回、解析と同じ
指示（`ANALYSIS_INSTRUCTION`）・同じスキーマ（`ArticleAnalysis`）で `ClaudeCliProvider`
を呼ぶ。結果は保存しない。`analysis.service.analyze_article` は結果を DB へ書くため
使わない。

LLM 呼び出しは失敗することがある（実測で 30 回中 2 回）。失敗した記事は比較不能として
記録し、他の記事の計測は続ける。リトライは挟まない（`run_llm_latency.py` と同じ理由:
リトライを混ぜると失敗の実態が読めなくなる）。

## 対照モード（`--control`）

LLM は同じ入力でも実行ごとに出力が揺れる。通常モードで観測される差が「切り捨てのせい」
なのか「実行ごとのばらつき」なのかを切り分けるため、`--control` を付けると切り捨てを
行わず全文を 2 回解析して比較する。これが実行ごとのばらつきのベースラインになる。
実測（Issue #73 追補）では、本文長が上限をわずか 0.9% 超えるだけの記事でも
topics の Jaccard が 0.43 まで落ちており、ばらつきが支配的である可能性が高いと分かった。

比較関数・集計関数・出力の形式は通常モードと同じものを使う（同じ土俵で数字を
並べられるようにするため）。対象記事の選び方も通常モードと同じにする（同じ記事集合で
両モードを比べたいため）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.analysis.prompt import ANALYSIS_INSTRUCTION
from techradar.analysis.schema import ArticleAnalysis
from techradar.analysis.service import MAX_ANALYSIS_BODY_CHARACTERS
from techradar.config import get_settings
from techradar.db.models import Article
from techradar.llm.base import LLMProvider
from techradar.llm.claude_cli import ClaudeCliProvider
from techradar.llm.errors import LLMManagedPolicyDetectedError, LLMToolUseDetectedError
from techradar.measure.cli_support import MeasurementArticle
from techradar.measure.cli_support import parse_article_count as _parse_article_count
from techradar.measure.cli_support import truncate_url as _truncate_url
from techradar.measure.session import read_only_session
from techradar.measure.truncation_impact import (
    TruncationImpact,
    compare_analyses,
    summarize_truncation_impacts,
)

DEFAULT_ARTICLES = 3


@dataclass(frozen=True)
class ComparisonFailure:
    """LLM 呼び出し失敗の記録。

    原因の切り分けに使うため、例外の型とメッセージの両方を残す（握りつぶさない）。
    """

    exception_type: str
    message: str


@dataclass(frozen=True)
class ArticleComparisonResult:
    """1 記事分の比較結果。

    成功時は `impact` を持ち `failure` は None、失敗時はその逆になる。
    """

    article: MeasurementArticle
    limit: int
    impact: TruncationImpact | None
    failure: ComparisonFailure | None


def _parse_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        message = f"上限は整数で指定してください: {value}"
        raise argparse.ArgumentTypeError(message) from exc
    if limit < 1:
        message = f"上限は1以上の整数で指定してください: {value}"
        raise argparse.ArgumentTypeError(message)
    return limit


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m techradar.measure.run_truncation_impact",
        description="切り捨てが解析結果へ与える影響を実測する（Issue #73）",
    )
    parser.add_argument(
        "--articles",
        type=_parse_article_count,
        default=DEFAULT_ARTICLES,
        help=(f"上限を超える記事のうち本文長が短い順に何件測るか（既定: {DEFAULT_ARTICLES}）"),
    )
    parser.add_argument(
        "--limit",
        type=_parse_limit,
        default=MAX_ANALYSIS_BODY_CHARACTERS,
        help=(
            "「切り捨て版」の本文長。対象記事もこの値を超える記事から選ぶ"
            f"（既定: {MAX_ANALYSIS_BODY_CHARACTERS}）"
        ),
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help=("対照モード。切り捨てず全文を2回解析し、実行ごとのばらつきをベースラインとして測る"),
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON で出力する")
    return parser.parse_args(argv)


def _load_measurement_bodies(
    session: Session, *, limit: int, count: int
) -> tuple[MeasurementArticle, ...]:
    """上限を超える記事を、本文長が短い順に選ぶ。

    上限を超えない記事は解析時にそもそも切り捨てられないため、比較対象にならない。
    賄える記事の中から短い順に選ぶことで、巨大な外れ値を引かないようにする
    （`run_llm_latency.py` の `_load_measurement_bodies` と同じ理由）。対照モードでも
    同じ選び方を使う（通常モードと同じ記事集合で比較したいため）。
    """
    stmt = (
        select(Article.canonical_url, Article.body)
        .where(Article.body.is_not(None))
        .where(func.length(Article.body) > limit)
        .order_by(func.length(Article.body).asc())
        .limit(count)
    )
    return tuple(
        MeasurementArticle(canonical_url=canonical_url, body=body)
        for canonical_url, body in session.execute(stmt).all()
    )


def _call(provider: LLMProvider, text: str) -> ArticleAnalysis:
    completion = provider.complete_json(
        instruction=ANALYSIS_INSTRUCTION,
        untrusted_content=text,
        schema=ArticleAnalysis,
    )
    return ArticleAnalysis.model_validate(completion.data)


def _mode_label(*, control: bool) -> str:
    if control:
        return "対照モード（全文版 vs 全文版、実行ごとのばらつきのベースライン）"
    return "通常モード（切り捨て版 vs 全文版）"


def _first_variant_label(*, control: bool) -> str:
    return "全文版(1回目)" if control else "切り捨て版"


def _second_variant_label(*, control: bool) -> str:
    return "全文版(2回目)" if control else "全文版"


def _compare_article(
    provider: LLMProvider,
    article: MeasurementArticle,
    *,
    limit: int,
    index: int,
    total: int,
    control: bool = False,
) -> ArticleComparisonResult:
    """1 記事について 2 回解析し、結果を比較する。

    通常モード（`control=False`）は「切り捨て版」→「全文版」の順、対照モード
    （`control=True`）は全文を 2 回解析する。対照モードは実行ごとのばらつきが
    結果へどれだけ効くかのベースラインを測るためのもの（Issue #73 追補）。

    どちらかが失敗した時点で打ち切り、比較不能として記録する。もう一方を呼んでも
    比較できないため無駄になる。
    """
    label = _truncate_url(article.canonical_url)
    first_body = article.body if control else article.body[:limit]
    first_variant = _first_variant_label(control=control)
    second_variant = _second_variant_label(control=control)

    print(f"  [{index}/{total}] {label} {first_variant}を解析中...", file=sys.stderr)
    try:
        first_analysis = _call(provider, first_body)
    except (LLMToolUseDetectedError, LLMManagedPolicyDetectedError):
        # 隔離破りの検知シグナル。握りつぶさず、比較不能として記録する代わりに
        # そのまま送出して計測を止める（ADR 0002）。
        raise
    except Exception as exc:
        print(
            f"  [{index}/{total}] {label} {first_variant}が失敗: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ArticleComparisonResult(
            article=article,
            limit=limit,
            impact=None,
            failure=ComparisonFailure(exception_type=type(exc).__name__, message=str(exc)),
        )

    print(f"  [{index}/{total}] {label} {second_variant}を解析中...", file=sys.stderr)
    try:
        second_analysis = _call(provider, article.body)
    except (LLMToolUseDetectedError, LLMManagedPolicyDetectedError):
        raise
    except Exception as exc:
        print(
            f"  [{index}/{total}] {label} {second_variant}が失敗: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ArticleComparisonResult(
            article=article,
            limit=limit,
            impact=None,
            failure=ComparisonFailure(exception_type=type(exc).__name__, message=str(exc)),
        )

    impact = compare_analyses(first_analysis, second_analysis)
    print(f"  [{index}/{total}] {label} 完了", file=sys.stderr)
    return ArticleComparisonResult(article=article, limit=limit, impact=impact, failure=None)


def _format_match(matches: bool) -> str:
    return "一致" if matches else "不一致"


def _render_article(result: ArticleComparisonResult, *, control: bool) -> list[str]:
    label = _truncate_url(result.article.canonical_url)
    first_variant = _first_variant_label(control=control)
    second_variant = _second_variant_label(control=control)
    lines = [f"記事: {label}（本文長 {len(result.article.body)} 文字 / 上限 {result.limit} 文字）"]

    if result.failure is not None:
        lines.append(f"  比較不能: {result.failure.exception_type}: {result.failure.message}")
        return lines

    impact = result.impact
    if impact is None:
        # `failure` が None のときは `impact` を必ず持たせている（`_compare_article` 参照）。
        # 破れているなら早期に落として気付けるようにする。
        message = "impact も failure も無い比較結果です"
        raise ValueError(message)
    lines.extend(
        [
            f"  domain: {_format_match(impact.domain_matches)}",
            f"  category: {_format_match(impact.category_matches)}",
            f"  content_type: {_format_match(impact.content_type_matches)}",
            f"  difficulty: {_format_match(impact.difficulty_matches)}",
            f"  topics Jaccard: {impact.topics_jaccard:.2f}",
            f"  technologies Jaccard: {impact.technologies_jaccard:.2f}",
            f"  technical_quality 差: {impact.technical_quality_diff:.2f}",
            f"  translated_title: {_format_match(impact.translated_title_matches)}",
            f"    {first_variant}: {impact.truncated_translated_title!r}",
            f"    {second_variant}: {impact.full_translated_title!r}",
            f"  summary_ja: {_format_match(impact.summary_matches)}",
            f"    {first_variant}: {impact.truncated_summary_ja}",
            f"    {second_variant}: {impact.full_summary_ja}",
        ]
    )
    return lines


def _render_summary(results: Sequence[ArticleComparisonResult]) -> list[str]:
    impacts = [result.impact for result in results if result.impact is not None]
    failed_count = sum(1 for result in results if result.failure is not None)
    summary = summarize_truncation_impacts(impacts, failed_count=failed_count)

    lines = [
        f"全体（記事 {len(results)} 件中 比較 {summary.compared_count} 件 / "
        f"失敗 {summary.failed_count} 件）:"
    ]
    if summary.compared_count == 0:
        lines.append("  比較できた記事がありません")
        return lines

    def _rate(value: float | None) -> str:
        return "-" if value is None else f"{value * 100:.1f}%"

    def _median(value: float | None) -> str:
        return "-" if value is None else f"{value:.2f}"

    lines.extend(
        [
            f"  domain 一致率: {_rate(summary.domain_match_rate)}",
            f"  category 一致率: {_rate(summary.category_match_rate)}",
            f"  content_type 一致率: {_rate(summary.content_type_match_rate)}",
            f"  difficulty 一致率: {_rate(summary.difficulty_match_rate)}",
            f"  topics Jaccard 中央値: {_median(summary.topics_jaccard_median)}",
            f"  technologies Jaccard 中央値: {_median(summary.technologies_jaccard_median)}",
            f"  technical_quality 差の中央値: {_median(summary.technical_quality_diff_median)}",
        ]
    )
    return lines


def _render_text(results: Sequence[ArticleComparisonResult], *, control: bool = False) -> str:
    lines: list[str] = [f"モード: {_mode_label(control=control)}", ""]
    for result in results:
        lines.extend(_render_article(result, control=control))
        lines.append("")
    lines.extend(_render_summary(results))
    return "\n".join(lines)


def _render_json(results: Sequence[ArticleComparisonResult], *, control: bool = False) -> str:
    impacts = [result.impact for result in results if result.impact is not None]
    failed_count = sum(1 for result in results if result.failure is not None)
    payload = {
        "control": control,
        "articles": [
            {
                "canonical_url": result.article.canonical_url,
                "body_length": len(result.article.body),
                "limit": result.limit,
                "failure": asdict(result.failure) if result.failure else None,
                "impact": asdict(result.impact) if result.impact else None,
            }
            for result in results
        ],
        "summary": asdict(summarize_truncation_impacts(impacts, failed_count=failed_count)),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    with read_only_session() as session:
        articles = _load_measurement_bodies(session, limit=args.limit, count=args.articles)
    if not articles:
        print(
            "測れる本文がありません（上限を超える記事が無いか、記事が未取り込みです）",
            file=sys.stderr,
        )
        return 1

    provider = ClaudeCliProvider(settings)
    results = [
        _compare_article(
            provider,
            article,
            limit=args.limit,
            index=index,
            total=len(articles),
            control=args.control,
        )
        for index, article in enumerate(articles, start=1)
    ]

    if args.as_json:
        print(_render_json(results, control=args.control))
    else:
        print(_render_text(results, control=args.control))
    return 0


if __name__ == "__main__":
    sys.exit(main())
