"""直近 N 日フィルタと件数上限（`techradar.collectors.filters`）を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from techradar.collectors.base import CandidateArticle
from techradar.collectors.filters import filter_recent, limit_candidates

FRESHNESS_DAYS = 7
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _candidate(
    *,
    published_at: datetime | None,
    url: str = "https://example.com/a",
    title: str = "title",
) -> CandidateArticle:
    return CandidateArticle(
        url=url,
        title=title,
        published_at=published_at,
        collector_name="rss",
    )


class TestFilterRecent:
    def test_keeps_a_candidate_published_exactly_freshness_days_ago(self):
        # Arrange — 境界値: ちょうど 7 日前は含む（>=）
        candidate = _candidate(published_at=NOW - timedelta(days=FRESHNESS_DAYS))

        # Act
        result = filter_recent((candidate,), freshness_days=FRESHNESS_DAYS, now=NOW)

        # Assert
        assert result == (candidate,)

    def test_excludes_a_candidate_published_one_second_before_the_boundary(self):
        # Arrange — 境界値: 7 日 + 1 秒前は除外
        candidate = _candidate(
            published_at=NOW - timedelta(days=FRESHNESS_DAYS) - timedelta(seconds=1)
        )

        # Act
        result = filter_recent((candidate,), freshness_days=FRESHNESS_DAYS, now=NOW)

        # Assert
        assert result == ()

    def test_excludes_a_candidate_with_no_published_at(self):
        # Arrange — 日付不明の候補を無制限に通すとコストが暴走するため除外
        candidate = _candidate(published_at=None)

        # Act
        result = filter_recent((candidate,), freshness_days=FRESHNESS_DAYS, now=NOW)

        # Assert
        assert result == ()

    def test_keeps_a_candidate_published_in_the_future(self):
        # Arrange — フィード側の時刻ずれで取りこぼさないよう未来日付は残す
        candidate = _candidate(published_at=NOW + timedelta(days=1))

        # Act
        result = filter_recent((candidate,), freshness_days=FRESHNESS_DAYS, now=NOW)

        # Assert
        assert result == (candidate,)

    def test_excludes_a_candidate_with_a_naive_published_at(self):
        # Arrange — タイムゾーン無し（naive）は判定不能として除外する設計
        candidate = _candidate(published_at=datetime(2026, 8, 1, 12, 0, 0))

        # Act
        result = filter_recent((candidate,), freshness_days=FRESHNESS_DAYS, now=NOW)

        # Assert
        assert result == ()

    def test_defaults_now_to_the_current_time_when_omitted(self):
        # Arrange — now 省略時は datetime.now(UTC) を使う
        candidate = _candidate(published_at=datetime.now(UTC))

        # Act
        result = filter_recent((candidate,), freshness_days=FRESHNESS_DAYS)

        # Assert
        assert result == (candidate,)


class TestLimitCandidates:
    def test_keeps_the_newest_candidates_up_to_the_limit(self):
        # Arrange — 新しい順に max_candidates 件だけ残す
        oldest = _candidate(published_at=NOW - timedelta(days=3), url="https://example.com/old")
        middle = _candidate(published_at=NOW - timedelta(days=1), url="https://example.com/mid")
        newest = _candidate(published_at=NOW, url="https://example.com/new")

        # Act
        result = limit_candidates((oldest, newest, middle), max_candidates=2)

        # Assert
        assert result == (newest, middle)

    def test_returns_all_candidates_when_under_the_limit(self):
        # Arrange / Act
        candidate = _candidate(published_at=NOW)
        result = limit_candidates((candidate,), max_candidates=10)

        # Assert
        assert result == (candidate,)

    def test_returns_empty_when_max_candidates_is_zero_or_negative(self):
        # Arrange / Act / Assert — 上限 0 以下は何も残さない
        candidate = _candidate(published_at=NOW)
        assert limit_candidates((candidate,), max_candidates=0) == ()
        assert limit_candidates((candidate,), max_candidates=-1) == ()

    def test_treats_missing_or_naive_published_at_as_oldest(self):
        # Arrange — None・naive は最も古い扱いで末尾に回す（例外にしない）
        unknown = _candidate(published_at=None, url="https://example.com/unknown")
        naive = _candidate(
            published_at=datetime(2026, 8, 1, 12, 0, 0), url="https://example.com/naive"
        )
        known = _candidate(published_at=NOW, url="https://example.com/known")

        # Act
        result = limit_candidates((unknown, naive, known), max_candidates=3)

        # Assert
        assert result[0] == known
        assert set(result[1:]) == {unknown, naive}
