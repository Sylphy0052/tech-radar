"""候補記事の直近 N 日フィルタと件数上限（`PROJECT_SPEC.md` §12）。

巡回で見つかった候補は玉石混交のため、後続の fetch・LLM 解析へ回す前にここで
絞り込む。フィルタを通った候補だけが以降の処理コストを消費するため、
判定基準はコストの安全弁として機能する（Issue #9 T12）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from techradar.collectors.base import CandidateArticle

# 公開日不明な候補を並べ替えの最下位に落とすための目印。`published_at` が
# aware datetime のときだけ比較できるよう、naive な `datetime.min` ではなく
# UTC 付きの値を使う（naive と aware を比較すると例外になるため）。
_MIN_DATETIME = datetime.min.replace(tzinfo=UTC)


def filter_recent(
    candidates: Sequence[CandidateArticle],
    *,
    freshness_days: int,
    now: datetime | None = None,
) -> tuple[CandidateArticle, ...]:
    """公開日が直近 `freshness_days` 日以内の候補だけを残す。

    - `published_at` が None の候補は除外する。7 日フィルタを通せない候補を
      素通りさせると、日付不明の古い記事が無制限に流入し、fetch・LLM 解析の
      コストを無駄に費やすことになる。取りこぼしは許容する設計判断
      （本 Issue で確定）。
    - タイムゾーン無し（naive）の `published_at` も判定不能として除外する。
      naive のまま `now`（aware）と比較すると例外になるうえ、どのタイム
      ゾーンの時刻か分からない値を「直近」と断定するのは危険なため。
    - 未来日付の候補は残す。フィード側の時刻ずれ（配信元サーバーの時計
      ずれやタイムゾーン変換ミス）で、実際には直近の記事を誤って
      取りこぼさないようにするため。
    - 境界は「ちょうど `freshness_days` 日前」を含む（`>=`）。
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=freshness_days)
    return tuple(
        candidate
        for candidate in candidates
        if candidate.published_at is not None
        and candidate.published_at.tzinfo is not None
        and candidate.published_at >= cutoff
    )


def limit_candidates(
    candidates: Sequence[CandidateArticle],
    *,
    max_candidates: int,
) -> tuple[CandidateArticle, ...]:
    """1 回の巡回で扱う候補数を上限で切る（コスト暴走の安全弁）。

    公開日の降順（新しいもの優先）で `max_candidates` 件だけ残す。
    `max_candidates` が 0 以下なら何も残さない。
    """
    if max_candidates <= 0:
        return ()

    ordered = sorted(candidates, key=_sort_key, reverse=True)
    return tuple(ordered[:max_candidates])


def _sort_key(candidate: CandidateArticle) -> datetime:
    """`limit_candidates` の並べ替えキー。

    `filter_recent` を経由していれば `published_at` は必ず aware datetime だが、
    `limit_candidates` は単体でも呼べる API のため、None や naive datetime が
    紛れ込んでも例外にせず最下位（最も古い扱い）として扱う。
    """
    published_at = candidate.published_at
    if published_at is None or published_at.tzinfo is None:
        return _MIN_DATETIME
    return published_at
