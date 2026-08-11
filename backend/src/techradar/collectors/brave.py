"""Brave Search API を巡回する候補記事コレクター（`PROJECT_SPEC.md` §12）。

Brave Search の無料枠は月 2000 クエリ・1 qps が上限のため、クエリ数と
リクエスト間隔の両方を安全弁で抑える。API キー未設定でも巡回全体を
止めないよう、`collect()` は例外を出さず空リストを返す。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from techradar.collectors.base import CandidateArticle
from techradar.config import Settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import JSON_CONTENT_TYPES, fetch_resource

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

# Brave 無料枠は 1 qps が上限。連続リクエストの間隔がこれを下回らないよう待機する。
MIN_REQUEST_INTERVAL_SECONDS = 1.0

# 1 回の collect() で投げるクエリ数の上限。月次クォータ（2000 クエリ/月）を
# 巡回のたびに使い切らないための安全弁。
MAX_QUERIES_PER_COLLECT = 10

# Brave Search API の認証ヘッダ名（値ではなくヘッダキー名の定数）。
_SUBSCRIPTION_TOKEN_HEADER = "X-Subscription-Token"  # noqa: S105 — 秘密値ではなくヘッダ名


class BraveSearchCollector:
    """Brave Search API を巡回するコレクター。

    検索クエリの生成はこのクラスの責務ではない。呼び出し側
    （`collectors/query.py`、別担当実装）が組み立てたクエリ列を
    コンストラクタで受け取るだけにする。
    """

    name: str = "brave_search"

    def __init__(
        self,
        queries: Sequence[str],
        settings: Settings,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queries = tuple(queries)
        self._settings = settings
        # 実時間を待たず・テストから差し替えられるよう、待機と時刻取得を注入可能にする。
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    def collect(self) -> Sequence[CandidateArticle]:
        """設定されたクエリを上限件数まで巡回し、候補記事をまとめて返す。

        API キー未設定時は例外を出さず空リストを返す（受入基準）。1 クエリの
        取得・パース失敗は警告ログに残してスキップし、他のクエリの収集は続ける。
        """
        if not self._settings.is_brave_search_enabled:
            logger.info("Brave Search の API キーが未設定のため収集をスキップします")
            return ()

        candidates: list[CandidateArticle] = []
        for query in self._queries[:MAX_QUERIES_PER_COLLECT]:
            candidates.extend(self._collect_query(query))
        return tuple(candidates)

    def _collect_query(self, query: str) -> list[CandidateArticle]:
        """1 クエリ分を取得・パースし、候補記事のリストを返す（失敗時は空）。"""
        self._wait_for_rate_limit()

        api_key = self._settings.brave_search_api_key
        if api_key is None:
            # `collect()` が `is_brave_search_enabled` で判定済みのため通常は
            # 起きないが、型チェッカーへ None でないことを伝えるためにも
            # 明示的に検査しておく。
            logger.warning("Brave Search の API キーが取得できませんでした")
            return []

        url = f"{BRAVE_SEARCH_URL}?{urlencode({'q': query})}"
        try:
            resource = fetch_resource(
                url,
                allowed_content_types=JSON_CONTENT_TYPES,
                headers={_SUBSCRIPTION_TOKEN_HEADER: api_key},
                settings=self._settings,
            )
        except FetchError as exc:
            # API キーをログへ出さないため、例外メッセージのみを記録する
            # （`FetchError` はリクエスト URL・ヘッダを含めない設計になっている）。
            logger.warning("Brave Search の取得に失敗しました: %s", exc)
            return []

        return self._parse_response(resource.text, query)

    def _wait_for_rate_limit(self) -> None:
        """前回リクエストから `MIN_REQUEST_INTERVAL_SECONDS` 以上空くまで待つ。

        1 回目のリクエストは前回が無いため待機しない。
        """
        if self._last_request_at is None:
            self._last_request_at = self._monotonic()
            return

        elapsed = self._monotonic() - self._last_request_at
        remaining = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_request_at = self._monotonic()

    def _parse_response(self, text: str, query: str) -> list[CandidateArticle]:
        """レスポンス本文をパースし、候補記事のリストを返す（壊れていれば空）。

        外部 API の応答は信用しない。期待した構造（`web.results` が配列で
        各要素が辞書）でない場合は例外にせず、スキップして警告ログに残す。
        """
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("Brave Search のレスポンスが JSON として不正です: %s (%s)", query, exc)
            return []

        if not isinstance(payload, dict):
            logger.warning("Brave Search のレスポンス構造が想定外です: %s", query)
            return []

        web = payload.get("web")
        if not isinstance(web, dict):
            return []
        results = web.get("results")
        if not isinstance(results, list):
            return []

        return [
            candidate
            for result in results
            if (candidate := self._to_candidate(result, query)) is not None
        ]

    def _to_candidate(self, result: Any, query: str) -> CandidateArticle | None:
        """1 検索結果を `CandidateArticle` へ変換する。作れない場合は None。"""
        if not isinstance(result, dict):
            return None
        url = result.get("url")
        if not isinstance(url, str) or not url:
            return None
        title = result.get("title")
        if not isinstance(title, str) or not title.strip():
            return None

        return CandidateArticle(
            url=url,
            title=title.strip(),
            published_at=_parse_page_age(result.get("page_age")),
            collector_name=self.name,
            source_hint=query,
        )


def _parse_page_age(page_age: Any) -> datetime | None:
    """`page_age`（ISO 8601 文字列、無いことがある）を UTC の datetime へ変換する。

    形式が壊れている場合も例外にせず None を返す（後段の鮮度フィルタが
    `None` を除外するため、現在時刻などで補完しない）。
    """
    if not isinstance(page_age, str) or not page_age:
        return None
    try:
        parsed = datetime.fromisoformat(page_age)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
