"""推薦 API のレート制限（`PROJECT_SPEC.md` §24, Issue #28）。

追加課金ゼロ・サーバー非常駐という制約と同様に、レート制限のためだけに外部
ストア（Redis 等）や追加依存（`slowapi` 等）を導入するのは単一ユーザー・
ローカル実行という前提に対して過剰と判断し、プロセス内メモリだけで完結する
スライディングウィンドウ方式を自前で実装する。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta
from math import ceil
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from techradar.api.deps import get_current_user_id, get_now
from techradar.config import Settings

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """キーごとにスライディングウィンドウでリクエスト数を制限する。

    状態はプロセス内メモリに持つ（キー→直近リクエスト時刻の `deque`）。FastAPI
    の同期エンドポイントはスレッドプールで実行される（イベントループとは別
    スレッドで呼ばれうる）ため、`threading.Lock` で状態を保護しスレッド安全に
    する。
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window = timedelta(seconds=window_seconds)
        self._lock = threading.Lock()
        # キーごとの直近リクエスト時刻。古い方が先頭に来るよう常に時刻順で
        # 追記するため、先頭から `popleft` するだけでウィンドウ外を捨てられる。
        self._requests: dict[str, deque[datetime]] = {}

    def check(self, key: str, now: datetime) -> float | None:
        """呼び出しを 1 回分記録し、制限超過なら待機秒数を返す。

        制限内であれば記録を追加したうえで `None` を返す。超過時は記録を
        追加しない（拒否された呼び出し自体をカウントすると、以降ずっと
        「超過」から抜け出せなくなるため）。待機秒数は、ウィンドウ内で
        最も古い記録がウィンドウを抜けるまでの残り秒数として計算する。
        """
        with self._lock:
            timestamps = self._requests.setdefault(key, deque())
            self._evict_expired(timestamps, now)

            if len(timestamps) >= self._max_requests:
                retry_after = (timestamps[0] + self._window) - now
                return max(retry_after.total_seconds(), 0.0)

            timestamps.append(now)
            return None

    def _evict_expired(self, timestamps: deque[datetime], now: datetime) -> None:
        """ウィンドウ外（`now - window` より前）の古い記録を捨てる。

        check のたびに行うことで、1 キーあたりの記録が `max_requests` 件を
        超えて溜まらないようにする。キー自体は一度作られると残るが、MVP は
        単一ユーザー（`default_user_id`）でキーが 1 つしか増えないため、
        未アクセスのキーを掃除する仕組みは設けない。
        """
        cutoff = now - self._window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()


def create_recommendation_rate_limiter(settings: Settings) -> SlidingWindowRateLimiter:
    """設定値からレート制限インスタンスを組み立てる。"""
    return SlidingWindowRateLimiter(
        max_requests=settings.recommendation_rate_limit_requests,
        window_seconds=settings.recommendation_rate_limit_window_seconds,
    )


def get_recommendation_rate_limiter(request: Request) -> SlidingWindowRateLimiter:
    """アプリ単位で 1 つ保持するレート制限インスタンスを返す。

    モジュールレベルのシングルトンにはしない。それだと `create_app` を複数回
    呼ぶテスト（`test_api_recommendations.py` は 1 テストごとに新しい app を
    作る）が全て同じインスタンスを共有してしまい、あるテストで消費した
    リクエスト回数が別のテストへ漏れる。`app.state.settings`（`api/deps.py` の
    `get_app_settings`）と同じく `create_app` が注入した `app.state` から読む
    ことで、app インスタンスと 1 対 1 の寿命に揃える。
    """
    limiter: SlidingWindowRateLimiter = request.app.state.recommendation_rate_limiter
    return limiter


def enforce_recommendation_rate_limit(
    request: Request,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    now: Annotated[datetime, Depends(get_now)],
) -> None:
    """推薦系エンドポイントへ適用する FastAPI dependency。

    制限キーは `user_id`。MVP は単一ユーザー（`default_user_id`）だが、将来
    認証を導入した際もそのまま利用者単位の制限として機能する。
    超過時は 429 を返し、`Retry-After` ヘッダへ待機秒数（整数秒へ切り上げ）を
    載せる。
    """
    limiter = get_recommendation_rate_limiter(request)
    wait_seconds = limiter.check(str(user_id), now)
    if wait_seconds is None:
        return

    retry_after_seconds = ceil(wait_seconds)
    logger.warning(
        "推薦 API のレート制限を超過しました: user_id=%s, retry_after=%s",
        user_id,
        retry_after_seconds,
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="リクエストが多すぎます。しばらく待ってから再試行してください",
        headers={"Retry-After": str(retry_after_seconds)},
    )
