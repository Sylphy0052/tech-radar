"""RSS/Atom フィードの巡回コレクター（`PROJECT_SPEC.md` §12）。

`config/feeds.yaml` の `rss` セクションに列挙された公式フィードを巡回し、
エントリを `CandidateArticle` へ変換する。`jp_media` セクションの巡回も実体は
同じ RSS/Atom パース処理のため、`techradar.collectors.jp_media.JpMediaCollector`
はこのモジュールの `RssCollector` を継承して再利用する（パース処理を複製
しない）。
"""

from __future__ import annotations

import calendar
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import feedparser

from techradar.collectors.base import CandidateArticle
from techradar.collectors.config import FeedEntryConfig
from techradar.config import Settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import FEED_CONTENT_TYPES, fetch_resource

logger = logging.getLogger(__name__)


def _to_utc_datetime(value: time.struct_time | None) -> datetime | None:
    """feedparser の `time.struct_time`（UTC 前提）を tz 付き `datetime` へ変換する。

    `published_parsed` / `updated_parsed` はどちらも feedparser が UTC で
    正規化した `time.struct_time`。ローカルタイムゾーンとして解釈する
    `time.mktime` ではなく、UTC 前提で epoch 秒へ変換する `calendar.timegm`
    を使う。
    """
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)


@dataclass(frozen=True)
class FeedFetchResult:
    """1 フィードぶんの巡回結果（Issue #105, #108）。

    `succeeded` は「取得でき、かつパースが破綻していない」こと（`RssCollector`
    docstring 参照）。`entry_count` はこのフィードから作れた候補記事の件数
    （`_to_candidate` で link/title 欠落として弾かれた分は含まない。「記事を
    配信しているか」を見る Issue #108 の用途では、パース上のエントリ数より、
    記事として扱える形になっている件数のほうが意味を持つため）。

    **重複・既存記事の除外より前の値である。** `collect_candidates` が後段で
    かける `filter_recent` / `_dedupe_by_normalized_url` /
    `_exclude_existing_articles` / `_exclude_already_queued` を通す前に数える
    ため、「毎回同じ既出記事だけを返すフィード」は `entry_count > 0` になる。
    Issue #108 が回収するのは「記事を配信しないフィード」の枠であって「新着が
    無いフィード」ではない。後者まで含めるなら別の指標（除外後の件数）が要る。

    取得・パースに失敗した場合は `entry_count=0` になるが、「記事が無かった」
    わけではなく「件数が分からない」ことを表す。呼び出し側
    （`discovery.record_feed_health`）は `succeeded=False` の行を「エントリ 0 件
    だった」とは数えない。
    """

    succeeded: bool
    entry_count: int


class RssCollector:
    """公式 RSS/Atom フィードを巡回するコレクター。

    フィード URL ごとの結果を `_feed_results` へ記録する（Issue #105, #108）。
    「成功」は「取得でき、かつパースが破綻していない（`bozo` かつエントリ 0 件、
    ではない）」こと。エントリが 0 件でもパースに成功していれば成功として扱う
    （フィードが生きていて記事が無いだけであり、フィード自体の不調ではないため）。
    「失敗」は `FetchError`（取得失敗）と、パースが破綻してエントリを 1 件も
    取れなかった場合の 2 通り。記録は DB を持たない（コレクターは DB を知らない
    ままにする、Issue #105 の設計判断）。呼び出し側（`collectors.service`）が
    `DiscoveredFeedCollector` のときだけこの記録を読んで DB へ反映する。
    """

    name: str = "rss"

    def __init__(self, feeds: Sequence[FeedEntryConfig], settings: Settings) -> None:
        self._feeds = tuple(feeds)
        self._settings = settings
        self._feed_results: dict[str, FeedFetchResult] = {}

    def collect(self) -> Sequence[CandidateArticle]:
        """設定された全フィードを巡回し、候補記事をまとめて返す。

        1 フィードの取得・パース失敗は警告ログに残してスキップし、他の
        フィードの収集は続ける。全フィードが失敗しても空リストを返してよい
        （呼び出し側の他コレクターを巻き込まないよう、ここでは
        `CollectorError` を送出しない。あくまでこのコレクター自体は
        「続行できる」ため）。

        呼び出しのたびに `_feed_results` をリセットしてから収集する。同じ
        インスタンスを2回呼んでも前回の記録が混ざらないようにするため。
        """
        self._feed_results = {}
        candidates: list[CandidateArticle] = []
        for feed in self._feeds:
            candidates.extend(self._collect_feed(feed))
        return tuple(candidates)

    def feed_results(self) -> dict[str, FeedFetchResult]:
        """直近の `collect()` におけるフィード URL ごとの結果を返す。

        キーはフィード URL、値は `FeedFetchResult`。`collect()` を呼ぶ前は空。
        """
        return dict(self._feed_results)

    def _collect_feed(self, feed: FeedEntryConfig) -> list[CandidateArticle]:
        """1 フィード分を取得・パースし、候補記事のリストを返す（失敗時は空）。"""
        try:
            # feedparser.parse() へ URL を直接渡すと feedparser 自身が名前解決・
            # HTTP 取得を行ってしまい、`techradar.fetcher.http` の SSRF ガード
            # （DNS ピンニング・リダイレクト毎の検証・レスポンスサイズ上限）を
            # 素通りする。必ず `fetch_resource` 経由で安全に取得済みのテキスト
            # だけを `feedparser.parse()` に渡す。
            resource = fetch_resource(
                feed.url,
                allowed_content_types=FEED_CONTENT_TYPES,
                settings=self._settings,
            )
        except FetchError as exc:
            logger.warning("フィードの取得に失敗しました: %s (%s)", feed.name, exc)
            self._feed_results[feed.url] = FeedFetchResult(succeeded=False, entry_count=0)
            return []

        parsed = feedparser.parse(resource.text)
        entries = parsed.entries
        if parsed.bozo and not entries:
            # パース自体が壊れていてエントリが 1 件も取れていない場合のみ
            # このフィードを諦める。1 件でも取れていれば bozo は警告に留める
            # （フィード側の軽微なマークアップ崩れで全滅させない）。
            logger.warning(
                "フィードのパースに失敗しました: %s (%s)",
                feed.name,
                parsed.get("bozo_exception"),
            )
            self._feed_results[feed.url] = FeedFetchResult(succeeded=False, entry_count=0)
            return []
        if parsed.bozo:
            logger.warning(
                "フィードの一部パースでエラーがありました（取得できた分のみ採用）: %s (%s)",
                feed.name,
                parsed.get("bozo_exception"),
            )

        candidates = [
            candidate
            for entry in entries
            if (candidate := self._to_candidate(entry, feed)) is not None
        ]
        self._feed_results[feed.url] = FeedFetchResult(succeeded=True, entry_count=len(candidates))
        return candidates

    def _to_candidate(self, entry: Any, feed: FeedEntryConfig) -> CandidateArticle | None:
        """1 エントリを `CandidateArticle` へ変換する。作れない場合は None。"""
        url = entry.get("link")
        if not url:
            # link の無いエントリは候補記事として登録できないためスキップする。
            return None
        title = (entry.get("title") or "").strip()
        if not title:
            # タイトル無しでは候補として扱えないためスキップする。
            return None

        published_at = _to_utc_datetime(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        return CandidateArticle(
            url=url,
            title=title,
            published_at=published_at,
            collector_name=self.name,
            source_hint=feed.name,
            feed_url=feed.url,
        )
