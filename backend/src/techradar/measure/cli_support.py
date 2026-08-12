"""計測エントリポイント間で重複していた補助関数（Issue #73 self review 対応）。

`run_llm_latency.py` と `run_truncation_impact.py` は同じ記事選定・表示処理を必要とする
（本文の代わりに canonical_url を持つ値オブジェクト、表示用 URL の切り詰め、`--articles`
オプションのパース）。それぞれで同じ定義を持たせると、直さなければならない箇所が2倍になる。
純粋関数のみをここへ置き、DB アクセスや LLM 呼び出しは持たせない。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

URL_DISPLAY_LIMIT = 80


@dataclass(frozen=True)
class MeasurementArticle:
    """計測対象の記事。`canonical_url` は表示にだけ使う。"""

    canonical_url: str
    body: str


def truncate_url(url: str, *, limit: int = URL_DISPLAY_LIMIT) -> str:
    """表示用に URL を短くする。長い URL は進捗行や表を潰すため。"""
    if len(url) <= limit:
        return url
    return url[: limit - 1] + "…"


def parse_article_count(value: str) -> int:
    """`--articles` オプションの値を検証する。1 以上の整数以外は argparse のエラーにする。"""
    try:
        count = int(value)
    except ValueError as exc:
        message = f"記事数は整数で指定してください: {value}"
        raise argparse.ArgumentTypeError(message) from exc
    if count < 1:
        message = f"記事数は1以上の整数で指定してください: {value}"
        raise argparse.ArgumentTypeError(message)
    return count
