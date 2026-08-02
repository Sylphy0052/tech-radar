"""スライディングウィンドウ方式のレート制限（`api/rate_limit.py`）を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from techradar.api.rate_limit import SlidingWindowRateLimiter

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class TestSlidingWindowRateLimiter:
    def test_allows_requests_within_the_limit(self) -> None:
        # Arrange
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60.0)

        # Act & Assert — 上限回数以内はすべて通る（待機不要 = None）
        for _ in range(3):
            assert limiter.check("user-1", NOW) is None

    def test_returns_wait_seconds_when_the_limit_is_exceeded(self) -> None:
        # Arrange — 上限 2 回をウィンドウ内で使い切る
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=60.0)
        limiter.check("user-1", NOW)
        limiter.check("user-1", NOW)

        # Act — 10 秒後に 3 回目を叩くと、最古の記録がウィンドウを抜けるまでの
        # 残り 50 秒を待機秒数として返す
        wait_seconds = limiter.check("user-1", NOW + timedelta(seconds=10))

        # Assert
        assert wait_seconds is not None
        assert wait_seconds == pytest.approx(50.0)

    def test_does_not_count_a_rejected_request_towards_the_limit(self) -> None:
        # Arrange — 上限超過時は記録を追加しない（拒否された呼び出し自体が
        # カウントを消費してしまうと、上限を超えた状態から抜け出せなくなる）
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
        limiter.check("user-1", NOW)
        limiter.check("user-1", NOW + timedelta(seconds=1))

        # Act
        wait_seconds = limiter.check("user-1", NOW + timedelta(seconds=2))

        # Assert — 最古の記録は最初の呼び出し（NOW）のままなので、待機秒数は
        # 2 回目の拒否呼び出しからではなく 1 回目の記録から計算される
        assert wait_seconds == pytest.approx(58.0)

    def test_allows_requests_again_after_the_window_passes(self) -> None:
        # Arrange
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
        limiter.check("user-1", NOW)
        assert limiter.check("user-1", NOW + timedelta(seconds=30)) is not None

        # Act — ウィンドウ（60 秒）を過ぎてから叩く
        result = limiter.check("user-1", NOW + timedelta(seconds=61))

        # Assert
        assert result is None

    def test_counts_different_keys_independently(self) -> None:
        # Arrange
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
        limiter.check("user-1", NOW)

        # Act — 別キーは独立にカウントされるため上限に達していない
        result = limiter.check("user-2", NOW)

        # Assert
        assert result is None

    def test_evicts_records_outside_the_window(self) -> None:
        # Arrange — 受入基準: ウィンドウ外の古い記録は check のたびに捨てられ、
        # 無制限にメモリが増えない
        limiter = SlidingWindowRateLimiter(max_requests=5, window_seconds=60.0)
        for offset in range(5):
            limiter.check("user-1", NOW + timedelta(seconds=offset))

        # Act — ウィンドウを過ぎてから同じキーで呼ぶ
        limiter.check("user-1", NOW + timedelta(seconds=200))

        # Assert — 古い 5 件は捨てられ、新しい 1 件だけが残る
        assert len(limiter._requests["user-1"]) == 1
