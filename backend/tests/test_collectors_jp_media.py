"""`JpMediaCollector` の挙動を検証する。"""

from __future__ import annotations

import pytest

from techradar.collectors import rss as rss_module
from techradar.collectors.config import FeedEntryConfig
from techradar.collectors.jp_media import JpMediaCollector
from techradar.config import Settings
from techradar.fetcher.http import FetchedResource

RSS2_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Zenn Trending</title>
<link>https://zenn.dev/</link>
<description>desc</description>
<item>
<title>国内技術メディアの記事</title>
<link>https://zenn.dev/articles/1</link>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""


def _resource(text: str) -> FetchedResource:
    return FetchedResource(
        final_url="https://zenn.dev/feed",
        body=text.encode("utf-8"),
        text=text,
        content_type="application/rss+xml",
        status_code=200,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def fake_fetch_resource(monkeypatch: pytest.MonkeyPatch) -> dict[str, FetchedResource]:
    """`fetch_resource` をスタブに差し替える（`RssCollector` の巡回先はモック済みテキスト）。"""
    responses: dict[str, FetchedResource] = {}

    def handler(
        url: str, *, allowed_content_types: tuple[str, ...], settings: Settings | None = None
    ) -> FetchedResource:
        return responses[url]

    monkeypatch.setattr(rss_module, "fetch_resource", handler)
    return responses


class TestJpMediaCollector:
    def test_returns_candidates_with_jp_media_collector_name(
        self, settings: Settings, fake_fetch_resource: dict[str, FetchedResource]
    ):
        # Arrange — Zenn 相当のフィードを 1 件用意する
        feed = FeedEntryConfig(name="Zenn Trending", url="https://zenn.dev/feed")
        fake_fetch_resource[feed.url] = _resource(RSS2_FEED)
        collector = JpMediaCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert — RssCollector と同じパース結果だが collector_name は jp_media
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.url == "https://zenn.dev/articles/1"
        assert candidate.title == "国内技術メディアの記事"
        assert candidate.collector_name == "jp_media"
        assert candidate.source_hint == feed.name

    def test_has_jp_media_as_its_name(self, settings: Settings):
        # Arrange / Act
        collector = JpMediaCollector([], settings)

        # Assert
        assert collector.name == "jp_media"
