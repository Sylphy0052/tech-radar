"""Hacker News からの候補記事収集（`PROJECT_SPEC.md` §12）。

Firebase が公開する Hacker News API（`https://hacker-news.firebaseio.com/v0/`）を使う。
認証不要で追加課金が発生しない。HTTP 通信は必ず
`techradar.fetcher.http.fetch_resource` 経由で行う。SSRF ガード（DNS
ピンニング・リダイレクト検証・レスポンスサイズ上限）を迂回しないためで、
`httpx` を直接使ったり自前で名前解決したりしてはならない。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from techradar.collectors.base import CandidateArticle, CollectorError
from techradar.collectors.config import FeedsConfig, get_feeds_config
from techradar.config import Settings, get_settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import JSON_CONTENT_TYPES, fetch_resource

logger = logging.getLogger(__name__)

# Firebase の Hacker News API。エンドポイントの組み立てはこのモジュール内に閉じる。
HACKER_NEWS_BASE_URL = "https://hacker-news.firebaseio.com/v0"
TOP_STORIES_URL = f"{HACKER_NEWS_BASE_URL}/topstories.json"
ITEM_URL_TEMPLATE = f"{HACKER_NEWS_BASE_URL}/item/{{item_id}}.json"

# 1 件の item 取得・パースで想定される例外。ネットワーク失敗（FetchError）と、
# 応答 JSON が壊れている・期待した構造でない場合（JSONDecodeError, KeyError,
# TypeError, ValueError）の両方を含む。外部 API の応答を信用せず、1 件の
# 失敗で巡回全体を止めないためにここでまとめて catch する。
_ITEM_ERRORS = (FetchError, json.JSONDecodeError, KeyError, TypeError, ValueError)


class HackerNewsCollector:
    """Hacker News の Top Stories から候補記事を収集する。"""

    name = "hacker_news"

    def __init__(
        self,
        *,
        feeds_config: FeedsConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._feeds_config = feeds_config
        self._settings = settings

    def collect(self) -> Sequence[CandidateArticle]:
        """Top Stories の先頭 `hacker_news_top_items` 件から候補記事を集める。

        Raises:
            CollectorError: Top Stories 一覧そのものが取得できなかった場合。
                以降の item 取得先が無く、この巡回では候補を作れないため。
        """
        feeds_config = self._feeds_config or get_feeds_config()
        settings = self._settings or get_settings()

        item_ids = self._fetch_top_story_ids(settings, feeds_config.hacker_news_top_items)

        candidates: list[CandidateArticle] = []
        for item_id in item_ids:
            candidate = self._fetch_candidate(item_id, settings)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _fetch_top_story_ids(self, settings: Settings, limit: int) -> list[int]:
        """Top Stories の ID 一覧を先頭 `limit` 件だけ返す。

        通信自体の失敗（ネットワーク層）は候補を 1 件も作れないことを意味するため
        `CollectorError` として呼び出し元へ伝播させる。応答は得られたが配列でない
        など構造が壊れている場合は、外部データを信用しない方針に従い例外にせず
        空リストとして扱う（巡回全体は継続させる）。
        """
        try:
            resource = fetch_resource(
                TOP_STORIES_URL, allowed_content_types=JSON_CONTENT_TYPES, settings=settings
            )
        except FetchError as exc:
            message = f"Hacker News の Top Stories を取得できません: {exc}"
            raise CollectorError(message) from exc

        try:
            payload: Any = json.loads(resource.body)
        except json.JSONDecodeError:
            logger.warning("Hacker News の Top Stories 応答が JSON として解釈できません")
            return []

        if not isinstance(payload, list):
            logger.warning("Hacker News の Top Stories 応答が配列ではありません")
            return []

        return [item_id for item_id in payload if isinstance(item_id, int)][:limit]

    def _fetch_candidate(self, item_id: int, settings: Settings) -> CandidateArticle | None:
        """1 件の item を取得して候補記事へ変換する。失敗したらログに残し None を返す。"""
        try:
            resource = fetch_resource(
                ITEM_URL_TEMPLATE.format(item_id=item_id),
                allowed_content_types=JSON_CONTENT_TYPES,
                settings=settings,
            )
            item: Any = json.loads(resource.body)
            return self._to_candidate(item)
        except _ITEM_ERRORS as exc:
            logger.warning("Hacker News item %s の取得をスキップします: %s", item_id, exc)
            return None

    def _to_candidate(self, item: Any) -> CandidateArticle | None:
        if not isinstance(item, dict):
            return None

        # Ask HN など self post は url を持たない。記事本文が HN 内部にしか
        # 無く候補記事として扱えないためスキップする。
        url = item.get("url")
        if not isinstance(url, str) or not url:
            return None

        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            return None

        return CandidateArticle(
            url=url,
            title=title,
            published_at=self._parse_published_at(item.get("time")),
            collector_name=self.name,
        )

    @staticmethod
    def _parse_published_at(unix_seconds: Any) -> datetime | None:
        if not isinstance(unix_seconds, int | float):
            return None
        return datetime.fromtimestamp(unix_seconds, tz=UTC)
