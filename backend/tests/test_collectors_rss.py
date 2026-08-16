"""`RssCollector` の巡回・パース挙動を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from techradar.collectors import rss as rss_module
from techradar.collectors.config import FeedEntryConfig
from techradar.collectors.rss import FeedFetchResult, RssCollector
from techradar.config import Settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import FEED_CONTENT_TYPES, FetchedResource

RSS2_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Test Feed</title>
<link>https://example.com/</link>
<description>desc</description>
<item>
<title>Sample Article</title>
<link>https://example.com/articles/1</link>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
</item>
<item>
<title>No Link Article</title>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
</item>
<item>
<title></title>
<link>https://example.com/articles/3</link>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
</item>
<item>
<title>No Date Article</title>
<link>https://example.com/articles/4</link>
</item>
</channel>
</rss>
"""

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Test Atom Feed</title>
<link href="https://example.com/"/>
<updated>2024-01-01T00:00:00Z</updated>
<entry>
<title>Atom Article</title>
<link href="https://example.com/articles/2"/>
<updated>2024-01-02T00:00:00Z</updated>
<id>urn:uuid:1</id>
</entry>
</feed>
"""

# パースは成功するがエントリが 0 件のフィード（フィードは生きているが記事が無いだけ）。
EMPTY_RSS2_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Empty Feed</title>
<link>https://example.com/</link>
<description>desc</description>
</channel>
</rss>
"""

# パース自体が壊れているフィード（bozo かつエントリ 0 件）。
BROKEN_FEED = "<not-a-feed>"


def _feed_entry(
    name: str = "Example Feed", url: str = "https://example.com/feed.xml"
) -> FeedEntryConfig:
    return FeedEntryConfig(name=name, url=url)


def _resource(text: str, *, content_type: str = "application/rss+xml") -> FetchedResource:
    return FetchedResource(
        final_url="https://example.com/feed.xml",
        body=text.encode("utf-8"),
        text=text,
        content_type=content_type,
        status_code=200,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class _FakeFetchResource:
    """テストごとに URL→応答の対応を積み上げるための小さなヘルパー。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str, object] = {}

    def set_response(self, url: str, resource: FetchedResource) -> None:
        self.responses[url] = resource

    def set_error(self, url: str, error: Exception) -> None:
        self.responses[url] = error

    def __call__(
        self, url: str, *, allowed_content_types: tuple[str, ...], settings: Settings | None = None
    ) -> FetchedResource:
        self.calls.append({"url": url, "allowed_content_types": allowed_content_types})
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FetchedResource)
        return result


@pytest.fixture
def fake_fetch_resource(monkeypatch: pytest.MonkeyPatch) -> _FakeFetchResource:
    """`fetch_resource` をスタブに差し替え、呼び出し引数を記録する。"""
    fake = _FakeFetchResource()
    monkeypatch.setattr(rss_module, "fetch_resource", fake)
    return fake


class TestCollectFromRss2Feed:
    def test_returns_a_candidate_with_url_title_and_published_at(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert
        matched = [c for c in candidates if c.url == "https://example.com/articles/1"]
        assert len(matched) == 1
        candidate = matched[0]
        assert candidate.title == "Sample Article"
        assert candidate.collector_name == "rss"
        assert candidate.source_hint == feed.name
        assert candidate.published_at == datetime(2024, 1, 1, tzinfo=UTC)

    def test_published_at_is_timezone_aware_utc(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert
        matched = next(c for c in candidates if c.url == "https://example.com/articles/1")
        assert matched.published_at is not None
        assert matched.published_at.tzinfo is UTC

    def test_skips_entries_without_a_link(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert — "No Link Article" は link が無いため除外される
        assert "No Link Article" not in [c.title for c in candidates]

    def test_skips_entries_with_an_empty_title(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert — タイトル空の記事（articles/3）は候補に含まれない
        assert "https://example.com/articles/3" not in [c.url for c in candidates]

    def test_entry_without_published_or_updated_has_none_published_at(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert — 日付が取れないだけでは除外しない（後段の鮮度フィルタに任せる）
        matched = next(c for c in candidates if c.url == "https://example.com/articles/4")
        assert matched.published_at is None


class TestCollectFromAtomFeed:
    def test_returns_a_candidate_using_updated_as_published_at(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(
            feed.url, _resource(ATOM_FEED, content_type="application/atom+xml")
        )
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.url == "https://example.com/articles/2"
        assert candidate.title == "Atom Article"
        assert candidate.published_at == datetime(2024, 1, 2, tzinfo=UTC)


class TestCollectResilience:
    def test_continues_when_one_feed_fetch_fails(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — 1本目は取得失敗、2本目は成功
        broken_feed = _feed_entry(name="Broken Feed", url="https://example.com/broken.xml")
        healthy_feed = _feed_entry(name="Healthy Feed", url="https://example.com/healthy.xml")
        fake_fetch_resource.set_error(broken_feed.url, FetchError("取得に失敗しました"))
        fake_fetch_resource.set_response(healthy_feed.url, _resource(RSS2_FEED))
        collector = RssCollector([broken_feed, healthy_feed], settings)

        # Act
        candidates = collector.collect()

        # Assert — 失敗したフィードを無視して、成功したフィードの候補は返る
        assert any(c.url == "https://example.com/articles/1" for c in candidates)

    def test_returns_empty_list_when_all_feeds_fail(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_error(feed.url, FetchError("取得に失敗しました"))
        collector = RssCollector([feed], settings)

        # Act / Assert
        assert collector.collect() == ()


class TestFetchResourceUsage:
    def test_calls_fetch_resource_with_feed_content_types(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — SSRF ガード経路（fetch_resource）を必ず通ることを担保する
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        collector.collect()

        # Assert
        assert len(fake_fetch_resource.calls) == 1
        call = fake_fetch_resource.calls[0]
        assert call["url"] == feed.url
        assert call["allowed_content_types"] == FEED_CONTENT_TYPES


class TestFeedResults:
    """フィード URL ごとの結果記録（Issue #105, #108）。`collect()` の戻り値は
    変えないまま、別メソッドで `FeedFetchResult`（成否とエントリ件数）を
    取り出せることを検証する。
    """

    def test_records_success_for_a_feed_that_was_fetched_and_parsed(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — RSS2_FEED の 4 item のうち link/title が揃っているのは 2 件
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        collector.collect()

        # Assert
        assert collector.feed_results() == {
            feed.url: FeedFetchResult(succeeded=True, entry_count=2)
        }

    def test_records_failure_when_fetch_raises_a_fetch_error(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_error(feed.url, FetchError("取得に失敗しました"))
        collector = RssCollector([feed], settings)

        # Act
        collector.collect()

        # Assert
        assert collector.feed_results() == {
            feed.url: FeedFetchResult(succeeded=False, entry_count=0)
        }

    def test_records_failure_when_parsing_is_bozo_with_no_entries(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — パース自体が壊れていてエントリが 1 件も取れない
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(BROKEN_FEED))
        collector = RssCollector([feed], settings)

        # Act
        collector.collect()

        # Assert
        assert collector.feed_results() == {
            feed.url: FeedFetchResult(succeeded=False, entry_count=0)
        }

    def test_records_success_when_parsed_but_has_zero_entries(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        """受入基準: エントリ 0 件でもパース成功なら失敗にしない
        （フィードが生きていて記事が無いだけのため）。`entry_count` も 0 になる
        （Issue #108 の連続空配信判定はこの値を見る）。
        """
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(EMPTY_RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        collector.collect()

        # Assert
        assert collector.feed_results() == {
            feed.url: FeedFetchResult(succeeded=True, entry_count=0)
        }

    def test_entry_count_reflects_deliverable_candidates_not_raw_entries(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        """受入基準: `entry_count` は link/title を欠いて配信できなかった item を
        含まない。パース上の生エントリ数（4）ではなく配信できた候補数（2）を返す
        （Issue #108、`RssCollector._to_candidate` が弾く分は「配信していない」ため）。
        """
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 2
        assert collector.feed_results()[feed.url].entry_count == len(candidates)

    def test_resets_results_when_collect_is_called_a_second_time(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        """受入基準: 同じインスタンスを2回呼んでも前回の記録が混ざらない。"""
        # Arrange — 1回目は失敗、2回目は同じフィードが成功する
        feed = _feed_entry()
        fake_fetch_resource.set_error(feed.url, FetchError("1回目は失敗"))
        collector = RssCollector([feed], settings)
        collector.collect()
        assert collector.feed_results() == {
            feed.url: FeedFetchResult(succeeded=False, entry_count=0)
        }

        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))

        # Act
        collector.collect()

        # Assert — 1回目の失敗記録が残っていない
        assert collector.feed_results() == {
            feed.url: FeedFetchResult(succeeded=True, entry_count=2)
        }


class TestCandidateFeedUrl:
    """候補記事から巡回元のフィード URL を逆引きできること（Issue #109）。

    「新着が出ないフィード」の判定は、除外を通り抜けた候補をフィード単位で
    数える（ADR 0008）。集計のキーには `record_feed_health` と同じ `feed_url`
    を使うため、候補側にも同じ値を持たせる。
    """

    def test_candidates_carry_the_feed_url_they_came_from(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        feed = _feed_entry()
        fake_fetch_resource.set_response(feed.url, _resource(RSS2_FEED))
        collector = RssCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert — 名前（`source_hint`）ではなく URL が入る
        assert [candidate.feed_url for candidate in candidates] == [feed.url, feed.url]

    def test_keeps_each_candidate_pointing_at_its_own_feed(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        """受入基準: 複数フィードを巡回しても、候補は自分の出どころを指す。"""
        # Arrange
        rss_feed = _feed_entry(name="RSS Feed", url="https://example.com/feed.xml")
        atom_feed = _feed_entry(name="Atom Feed", url="https://example.com/atom.xml")
        fake_fetch_resource.set_response(rss_feed.url, _resource(RSS2_FEED))
        fake_fetch_resource.set_response(atom_feed.url, _resource(ATOM_FEED))
        collector = RssCollector([rss_feed, atom_feed], settings)

        # Act
        candidates = collector.collect()

        # Assert
        feed_urls_by_article_url = {candidate.url: candidate.feed_url for candidate in candidates}
        assert feed_urls_by_article_url["https://example.com/articles/1"] == rss_feed.url
        assert feed_urls_by_article_url["https://example.com/articles/2"] == atom_feed.url
