"""記事本文長の集計（Issue #74、Issue #73 の前提）。

`analysis.service.MAX_ANALYSIS_BODY_CHARACTERS` は 12000 だが、その値の根拠が
残っていない。確定させるには、実際の記事がどれくらいの長さで、上限で切り捨てられる
記事がどれだけあるかを知る必要がある。

集計は「長さの列を受け取る純粋関数」と「DB から長さを読む関数」に分ける。値の正しさは
純粋関数側で固定でき、DB 側は読み取り内容だけを確かめればよくなる。
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.db.models import Article


@dataclass(frozen=True)
class BodyLengthStats:
    """本文長の分布。対象が 0 件なら長さ系は `None` になる。"""

    article_count: int
    min_length: int | None
    median_length: int | None
    max_length: int | None
    # `limit` を超える（= 解析時に切り捨てられる）記事の件数と割合。
    truncated_count: int
    truncated_ratio: float
    limit: int


def summarize_body_lengths(lengths: Sequence[int], limit: int) -> BodyLengthStats:
    """本文長の列から分布をまとめる。

    切り捨ての判定は `length > limit` にする。解析側は `body[:limit]` で切るため、
    ちょうど `limit` の記事は 1 文字も失われない。
    """
    if not lengths:
        return BodyLengthStats(
            article_count=0,
            min_length=None,
            median_length=None,
            max_length=None,
            truncated_count=0,
            truncated_ratio=0.0,
            limit=limit,
        )

    truncated_count = sum(1 for length in lengths if length > limit)
    return BodyLengthStats(
        article_count=len(lengths),
        min_length=min(lengths),
        # 件数が偶数だと中央 2 件の平均になり小数を含む。切り捨てると系統的に
        # 下振れするため丸める（`.5` は Python の既定どおり偶数側）。
        median_length=round(statistics.median(lengths)),
        max_length=max(lengths),
        truncated_count=truncated_count,
        truncated_ratio=truncated_count / len(lengths),
        limit=limit,
    )


def load_body_lengths(session: Session) -> tuple[int, ...]:
    """本文を持つ記事の本文長を読む。

    本文そのものは読まない。全件をメモリへ載せると、記事が増えたときに計測だけで
    大量のメモリを使う。長さは DB 側で数える。
    """
    stmt = select(func.length(Article.body)).where(Article.body.is_not(None))
    return tuple(int(length) for length in session.scalars(stmt))
