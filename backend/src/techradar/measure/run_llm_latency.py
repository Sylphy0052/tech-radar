"""本文長ごとの LLM 応答時間を実測する（Issue #73）。

    cd backend
    uv run python -m techradar.measure.run_llm_latency --lengths 2000,4000,8000,12000,16000

DB から代表記事を読み（読み取り専用）、指定の長さへ切って解析と同じ指示で
Claude Code CLI を呼び、所要時間を測る。応答時間はサブスク枠の混み具合に左右されるため、
`--repeats` で複数回測って中央値を見る。

代表記事は「測る最大長を賄える記事のうち、本文長が短い順」に `--articles` 件選ぶ。
本番 DB には全リリースノートを連結したような巨大な外れ値（数十万文字）が混ざっており、
最長の記事を選ぶと通常の技術記事の代表にならない。複数記事を測ることで、1 記事の
内容の難しさに結果が引きずられるのも避ける。

解析結果は保存しない。測りたいのは時間だけであり、`analysis.service.analyze_article` は
結果を DB へ書くため使わない。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.config import get_settings
from techradar.db.models import Article
from techradar.llm.base import LLMProvider
from techradar.llm.claude_cli import ClaudeCliProvider
from techradar.measure.llm_latency import (
    LatencySample,
    LatencyStats,
    measure_latency,
    summarize_latencies,
    take_prefixes,
)
from techradar.measure.session import read_only_session

DEFAULT_LENGTHS = (2000, 4000, 8000, 12000, 16000)
DEFAULT_REPEATS = 3
DEFAULT_ARTICLES = 2
_URL_DISPLAY_LIMIT = 80


@dataclass(frozen=True)
class MeasurementArticle:
    """計測対象の記事。`canonical_url` は表示にだけ使う。"""

    canonical_url: str
    body: str


@dataclass(frozen=True)
class ArticleLatencyResult:
    """1 記事分の計測結果（生のサンプルのまま持つ）。

    集計（中央値・成功数など）はここでは持たない。記事ごとの集計と、記事横断の
    全体集計の両方が必要で、どちらも同じ `summarize_latencies` を都度適用すれば
    足りるため、二重に持たせて食い違わせるより表示側で計算する。
    """

    article: MeasurementArticle
    samples: tuple[LatencySample, ...]


def _parse_lengths(value: str) -> tuple[int, ...]:
    lengths = []
    for part in value.split(","):
        text = part.strip()
        if not text:
            continue
        length = int(text)
        if length <= 0:
            message = f"本文長は正の整数で指定してください: {text}"
            raise argparse.ArgumentTypeError(message)
        lengths.append(length)
    if not lengths:
        message = "本文長を 1 つ以上指定してください"
        raise argparse.ArgumentTypeError(message)
    return tuple(lengths)


def _parse_article_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        message = f"記事数は整数で指定してください: {value}"
        raise argparse.ArgumentTypeError(message) from exc
    if count < 1:
        message = f"記事数は1以上の整数で指定してください: {value}"
        raise argparse.ArgumentTypeError(message)
    return count


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m techradar.measure.run_llm_latency",
        description="本文長ごとの LLM 応答時間を実測する（Issue #73）",
    )
    parser.add_argument(
        "--lengths",
        type=_parse_lengths,
        default=DEFAULT_LENGTHS,
        help=f"測る本文長をカンマ区切りで指定する（既定: {','.join(map(str, DEFAULT_LENGTHS))}）",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"各長さを何回測るか（既定: {DEFAULT_REPEATS}）",
    )
    parser.add_argument(
        "--articles",
        type=_parse_article_count,
        default=DEFAULT_ARTICLES,
        help=(
            "代表記事を何件測るか。測る最大長を賄える記事のうち短い順に選ぶ"
            f"（既定: {DEFAULT_ARTICLES}）"
        ),
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="JSON で出力する")
    return parser.parse_args(argv)


def _truncate_url(url: str, *, limit: int = _URL_DISPLAY_LIMIT) -> str:
    """表示用に URL を短くする。長い URL は進捗行や表を潰すため。"""
    if len(url) <= limit:
        return url
    return url[: limit - 1] + "…"


def _load_measurement_bodies(
    session: Session, *, min_length: int, count: int
) -> tuple[MeasurementArticle, ...]:
    """代表記事を、本文長が短い順に選ぶ。

    測る最大長（`min_length` に渡す）を賄えない記事は除く。賄える記事の中から短い順に
    選ぶことで、全リリースノートを連結したような巨大な外れ値（本番実測で 546409 文字）を
    引かないようにする（Issue #73）。絞り込みは DB 側の `length()` の order_by + limit で
    行い、対象外の記事の本文は読まない。
    """
    stmt = (
        select(Article.canonical_url, Article.body)
        .where(Article.body.is_not(None))
        .where(func.length(Article.body) >= min_length)
        .order_by(func.length(Article.body).asc())
        .limit(count)
    )
    return tuple(
        MeasurementArticle(canonical_url=canonical_url, body=body)
        for canonical_url, body in session.execute(stmt).all()
    )


def _measure_article(
    provider: LLMProvider,
    article: MeasurementArticle,
    *,
    lengths: Sequence[int],
    repeats: int,
    index: int,
    total: int,
) -> ArticleLatencyResult:
    """1 記事について、指定の長さ・回数だけ測る。

    進捗は呼び出しのたびに stderr へ出す。全体で数十分かかるため、どの記事のどの長さを
    測っているかが追えるようにする。
    """
    label = _truncate_url(article.canonical_url)
    samples: list[LatencySample] = []
    for length, text in take_prefixes(article.body, lengths):
        for _ in range(repeats):
            sample = measure_latency(provider, text=text, length=length)
            samples.append(sample)
            status = "ok" if sample.ok else "FAIL"
            print(
                f"  [{index}/{total}] {label} {length:>6} 文字: "
                f"{sample.seconds:6.1f} 秒 ({status})",
                file=sys.stderr,
            )
    return ArticleLatencyResult(article=article, samples=tuple(samples))


def _overall_stats(article_results: Sequence[ArticleLatencyResult]) -> tuple[LatencyStats, ...]:
    """記事をまたいだ全体集計。長さごとに全記事のサンプルをまとめる。"""
    samples = [sample for result in article_results for sample in result.samples]
    return summarize_latencies(samples)


def _render_stats_lines(stats: Sequence[LatencyStats]) -> list[str]:
    lines = []
    for stat in stats:
        if stat.median_seconds is None:
            lines.append(f"  {stat.length:>6} 文字: 全て失敗（{stat.failures} 回）")
            continue
        lines.append(
            f"  {stat.length:>6} 文字: 中央値 {stat.median_seconds:6.1f} 秒"
            f"（最小 {stat.min_seconds:.1f} / 最大 {stat.max_seconds:.1f}"
            f" / 成功 {stat.samples} / 失敗 {stat.failures}）"
        )
    return lines


def _render_text(article_results: Sequence[ArticleLatencyResult], *, repeats: int) -> str:
    lines: list[str] = []
    for result in article_results:
        lines.append(
            f"記事: {_truncate_url(result.article.canonical_url)}"
            f"（本文長 {len(result.article.body)} 文字 / 各長さ {repeats} 回）"
        )
        lines.extend(_render_stats_lines(summarize_latencies(result.samples)))
        lines.append("")

    lines.append(f"全体（記事 {len(article_results)} 件横断）:")
    lines.extend(_render_stats_lines(_overall_stats(article_results)))
    return "\n".join(lines)


def _render_json(article_results: Sequence[ArticleLatencyResult]) -> str:
    payload = {
        "articles": [
            {
                "canonical_url": result.article.canonical_url,
                "body_length": len(result.article.body),
                "stats": [asdict(stat) for stat in summarize_latencies(result.samples)],
            }
            for result in article_results
        ],
        "overall": [asdict(stat) for stat in _overall_stats(article_results)],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    with read_only_session() as session:
        articles = _load_measurement_bodies(
            session, min_length=max(args.lengths), count=args.articles
        )
    if not articles:
        print("測れる本文がありません。先に記事を取り込んでください")
        return 1

    provider = ClaudeCliProvider(settings)
    article_results = [
        _measure_article(
            provider,
            article,
            lengths=args.lengths,
            repeats=args.repeats,
            index=index,
            total=len(articles),
        )
        for index, article in enumerate(articles, start=1)
    ]

    if args.as_json:
        print(_render_json(article_results))
    else:
        print(_render_text(article_results, repeats=args.repeats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
