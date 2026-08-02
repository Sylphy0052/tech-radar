"""arXiv API を巡回する候補記事コレクター（`PROJECT_SPEC.md` §12）。

`config/feeds.yaml` の `arxiv_categories` に列挙されたカテゴリごとに、
新着順で記事を取得する。arXiv API の応答は Atom のため、パース処理自体は
`techradar.collectors.rss.RssCollector` と同様に `feedparser` を使うが、
クエリ組み立て・カテゴリ単位の巡回が RSS コレクターと異なるため独立した
モジュールにする。
"""

from __future__ import annotations

import calendar
import logging
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import feedparser

from techradar.collectors.base import CandidateArticle
from techradar.config import Settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import FEED_CONTENT_TYPES, fetch_resource

logger = logging.getLogger(__name__)

ARXIV_API_URL = "https://export.arxiv.org/api/query"

# 1 カテゴリあたりの取得件数。新着を拾えれば十分なため多くは要らないが、
# 巡回間隔中の投稿数が多いカテゴリでも取りこぼしにくい件数にしておく。
MAX_RESULTS_PER_CATEGORY = 20

# タイトル中の改行・連続空白をまとめて 1 個のスペースへ正規化する。
# arXiv のタイトルは Atom 上で表示幅に合わせて折り返され、改行が入るため。
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _to_utc_datetime(value: time.struct_time | None) -> datetime | None:
    """feedparser の `time.struct_time`（UTC 前提）を tz 付き `datetime` へ変換する。

    `published_parsed` / `updated_parsed` はどちらも feedparser が UTC で
    正規化した `time.struct_time`。ローカルタイムゾーンとして解釈する
    `time.mktime` ではなく、UTC 前提で epoch 秒へ変換する `calendar.timegm`
    を使う（`techradar.collectors.rss` と同じ変換）。
    """
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)


def _normalize_title(title: str) -> str:
    """タイトルの改行・連続空白を 1 個のスペースへ正規化して前後を trim する。"""
    return _WHITESPACE_PATTERN.sub(" ", title).strip()


def _build_query_url(category: str) -> str:
    """カテゴリから arXiv API のクエリ URL を組み立てる。

    値をそのまま f-string で連結すると、カテゴリ文字列に `&` 等が
    混じった場合にクエリが壊れたり意図しないパラメータ注入を許したりする
    ため、必ず `urlencode` でエスケープする。
    """
    params = {
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": MAX_RESULTS_PER_CATEGORY,
    }
    return f"{ARXIV_API_URL}?{urlencode(params)}"


class ArxivCollector:
    """arXiv API を巡回するコレクター。"""

    name: str = "arxiv"

    def __init__(self, categories: Sequence[str], settings: Settings) -> None:
        self._categories = tuple(categories)
        self._settings = settings

    def collect(self) -> Sequence[CandidateArticle]:
        """設定された全カテゴリを巡回し、候補記事をまとめて返す。

        1 カテゴリの取得・パース失敗は警告ログに残してスキップし、他の
        カテゴリの収集は続ける。全カテゴリが失敗しても空リストを返してよい
        （呼び出し側の他コレクターを巻き込まないよう、ここでは
        `CollectorError` を送出しない）。
        """
        candidates: list[CandidateArticle] = []
        for category in self._categories:
            candidates.extend(self._collect_category(category))
        return tuple(candidates)

    def _collect_category(self, category: str) -> list[CandidateArticle]:
        """1 カテゴリ分を取得・パースし、候補記事のリストを返す（失敗時は空）。"""
        try:
            # feedparser.parse() へ URL を直接渡すと feedparser 自身が名前解決・
            # HTTP 取得を行ってしまい、`techradar.fetcher.http` の SSRF ガード
            # （DNS ピンニング・リダイレクト毎の検証・レスポンスサイズ上限）を
            # 素通りする。必ず `fetch_resource` 経由で安全に取得済みのテキスト
            # だけを `feedparser.parse()` に渡す。
            resource = fetch_resource(
                _build_query_url(category),
                allowed_content_types=FEED_CONTENT_TYPES,
                settings=self._settings,
            )
        except FetchError as exc:
            logger.warning("arXiv カテゴリの取得に失敗しました: %s (%s)", category, exc)
            return []

        parsed = feedparser.parse(resource.text)
        entries = parsed.entries
        if parsed.bozo and not entries:
            # パース自体が壊れていてエントリが 1 件も取れていない場合のみ
            # このカテゴリを諦める。1 件でも取れていれば bozo は警告に留める。
            logger.warning(
                "arXiv カテゴリのパースに失敗しました: %s (%s)",
                category,
                parsed.get("bozo_exception"),
            )
            return []
        if parsed.bozo:
            logger.warning(
                "arXiv カテゴリの一部パースでエラーがありました（取得できた分のみ採用）: %s (%s)",
                category,
                parsed.get("bozo_exception"),
            )

        return [
            candidate
            for entry in entries
            if (candidate := self._to_candidate(entry, category)) is not None
        ]

    def _to_candidate(self, entry: Any, category: str) -> CandidateArticle | None:
        """1 エントリを `CandidateArticle` へ変換する。作れない場合は None。"""
        url = entry.get("link")
        if not url:
            # link の無いエントリは候補記事として登録できないためスキップする。
            return None
        title = _normalize_title(entry.get("title") or "")
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
            source_hint=category,
        )
