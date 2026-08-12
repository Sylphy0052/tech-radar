"""本文長ごとの LLM 応答時間の計測（Issue #73）。

`analysis.service.MAX_ANALYSIS_BODY_CHARACTERS` を確定するには、本文をどこまで渡すと
応答時間がどれだけ伸びるかを知る必要がある。同じ記事の本文を複数の長さへ切り、
解析と同じ指示・同じスキーマで LLM を呼んで所要時間を測る。

保存はしない。`analysis.service.analyze_article` は結果を DB へ書くため呼ばず、
プロバイダーを直接呼ぶ。リトライも挟まない（リトライを含めると待機時間が混ざり、
応答時間そのものが読めなくなる）。失敗は失敗として所要時間つきで残す。
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from techradar.analysis.prompt import ANALYSIS_INSTRUCTION
from techradar.analysis.schema import ArticleAnalysis
from techradar.llm.base import LLMProvider


@dataclass(frozen=True)
class LatencySample:
    """1 回の呼び出しの結果。`ok` が False なら LLM 呼び出しが失敗している。"""

    length: int
    seconds: float
    ok: bool


@dataclass(frozen=True)
class LatencyStats:
    """ある本文長の集計。成功した呼び出しだけで時間を出す。"""

    length: int
    samples: int
    median_seconds: float | None
    min_seconds: float | None
    max_seconds: float | None
    failures: int


def take_prefixes(text: str, lengths: Sequence[int]) -> tuple[tuple[int, str], ...]:
    """本文を指定の長さへ切り出す。

    本文より長い指定は捨てる。切っても同じ入力になり、同じものを二重に測ることになる。
    指定の順序は保つ（長い方から測りたい場合に呼び出し側で決められるようにする）。
    """
    if not text:
        return ()
    return tuple((length, text[:length]) for length in lengths if length <= len(text))


def measure_latency(
    provider: LLMProvider,
    *,
    text: str,
    length: int,
    clock: Callable[[], float] = time.monotonic,
) -> LatencySample:
    """1 回分の応答時間を測る。

    解析と同じ指示・スキーマで呼ぶ。失敗しても例外を投げず、失敗として記録する。
    測りたいのは「その長さで実用になるか」であり、落ちること自体が結果のため。
    """
    started = clock()
    ok = True
    try:
        provider.complete_json(
            instruction=ANALYSIS_INSTRUCTION,
            untrusted_content=text,
            schema=ArticleAnalysis,
        )
    except Exception:
        ok = False
    elapsed = clock() - started
    return LatencySample(length=length, seconds=elapsed, ok=ok)


def summarize_latencies(samples: Sequence[LatencySample]) -> tuple[LatencyStats, ...]:
    """本文長ごとにまとめる。

    時間の統計は成功した呼び出しだけで出す。途中で落ちた呼び出しを混ぜると、
    短く終わったぶん実際より速く見える。全部失敗した長さも、件数 0 として残す
    （「その長さでは測れなかった」ことが結果になる）。
    """
    by_length: dict[int, list[LatencySample]] = {}
    for sample in samples:
        by_length.setdefault(sample.length, []).append(sample)

    stats = []
    for length in sorted(by_length):
        entries = by_length[length]
        succeeded = [entry.seconds for entry in entries if entry.ok]
        stats.append(
            LatencyStats(
                length=length,
                samples=len(succeeded),
                median_seconds=statistics.median(succeeded) if succeeded else None,
                min_seconds=min(succeeded) if succeeded else None,
                max_seconds=max(succeeded) if succeeded else None,
                failures=sum(1 for entry in entries if not entry.ok),
            )
        )
    return tuple(stats)
