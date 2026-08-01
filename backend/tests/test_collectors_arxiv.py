"""`ArxivCollector` の巡回・パース挙動を検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from techradar.collectors import arxiv as arxiv_module
from techradar.collectors.arxiv import MAX_RESULTS_PER_CATEGORY, ArxivCollector
from techradar.config import Settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import FEED_CONTENT_TYPES, FetchedResource

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>arXiv Query</title>
<entry>
<id>http://arxiv.org/abs/2401.00001v1</id>
<title>  Example
   Paper Title  </title>
<link href="http://arxiv.org/abs/2401.00001v1" rel="alternate" type="text/html"/>
<published>2024-01-01T00:00:00Z</published>
<updated>2024-01-02T00:00:00Z</updated>
</entry>
<entry>
<id>http://arxiv.org/abs/2401.00002v1</id>
<title>No Link Paper</title>
<published>2024-01-01T00:00:00Z</published>
</entry>
</feed>
"""


def _resource(text: str, *, content_type: str = "application/atom+xml") -> FetchedResource:
    return FetchedResource(
        final_url="https://export.arxiv.org/api/query",
        body=text.encode("utf-8"),
        text=text,
        content_type=content_type,
        status_code=200,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class _FakeFetchResource:
    """テストごとに category→応答の対応を積み上げるための小さなヘルパー。

    `ArxivCollector` は URL をカテゴリから毎回組み立てるため、category 名で
    引き当てる（`urlparse` で `search_query` の `cat:` 部分から復元する）。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str, object] = {}

    def set_response(self, category: str, resource: FetchedResource) -> None:
        self.responses[category] = resource

    def set_error(self, category: str, error: Exception) -> None:
        self.responses[category] = error

    def __call__(
        self, url: str, *, allowed_content_types: tuple[str, ...], settings: Settings | None = None
    ) -> FetchedResource:
        self.calls.append({"url": url, "allowed_content_types": allowed_content_types})
        category = _category_from_url(url)
        result = self.responses[category]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FetchedResource)
        return result


def _category_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    search_query = query["search_query"][0]
    assert search_query.startswith("cat:")
    return search_query.removeprefix("cat:")


@pytest.fixture
def fake_fetch_resource(monkeypatch: pytest.MonkeyPatch) -> _FakeFetchResource:
    """`fetch_resource` をスタブに差し替え、呼び出し引数を記録する。"""
    fake = _FakeFetchResource()
    monkeypatch.setattr(arxiv_module, "fetch_resource", fake)
    return fake


class TestCollectFromArxivFeed:
    def test_returns_a_candidate_with_url_title_and_published_at(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.set_response("cs.SE", _resource(ATOM_FEED))
        collector = ArxivCollector(["cs.SE"], settings)

        # Act
        candidates = collector.collect()

        # Assert
        matched = [c for c in candidates if c.url == "http://arxiv.org/abs/2401.00001v1"]
        assert len(matched) == 1
        candidate = matched[0]
        assert candidate.title == "Example Paper Title"
        assert candidate.collector_name == "arxiv"
        assert candidate.source_hint == "cs.SE"
        assert candidate.published_at == datetime(2024, 1, 1, tzinfo=UTC)

    def test_normalizes_line_breaks_in_title_to_a_single_space(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — arXiv のタイトルは折り返しのため改行や連続空白が混じる
        fake_fetch_resource.set_response("cs.SE", _resource(ATOM_FEED))
        collector = ArxivCollector(["cs.SE"], settings)

        # Act
        candidates = collector.collect()

        # Assert
        candidate = next(c for c in candidates if c.url == "http://arxiv.org/abs/2401.00001v1")
        assert "\n" not in candidate.title
        assert "  " not in candidate.title
        assert candidate.title == "Example Paper Title"

    def test_skips_entries_without_a_link(self, settings: Settings):
        # Arrange — Atom の `id` は仕様上必須で feedparser が link のフォールバックに
        # 使うため、実際の応答経由では再現しにくい。変換処理そのものを直接呼んで
        # 「link が読み取れないエントリはスキップする」防御を検証する。
        collector = ArxivCollector(["cs.SE"], settings)

        # Act
        candidate = collector._to_candidate({"title": "No Link Paper"}, "cs.SE")

        # Assert
        assert candidate is None


class TestQueryString:
    def test_builds_the_query_string_with_urlencode_and_a_cat_prefix(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.set_response("cs.SE", _resource(ATOM_FEED))
        collector = ArxivCollector(["cs.SE"], settings)

        # Act
        collector.collect()

        # Assert — urlencode で組み立てられ、カテゴリが cat: 付きで入っている
        call = fake_fetch_resource.calls[0]
        url = str(call["url"])
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "export.arxiv.org"
        query = parse_qs(parsed.query)
        assert query["search_query"] == ["cat:cs.SE"]
        assert query["sortBy"] == ["submittedDate"]
        assert query["sortOrder"] == ["descending"]
        assert query["max_results"] == [str(MAX_RESULTS_PER_CATEGORY)]


class TestCollectResilience:
    def test_continues_when_one_category_fetch_fails(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — 1カテゴリ目は取得失敗、2カテゴリ目は成功
        fake_fetch_resource.set_error("cs.SE", FetchError("取得に失敗しました"))
        fake_fetch_resource.set_response("cs.CL", _resource(ATOM_FEED))
        collector = ArxivCollector(["cs.SE", "cs.CL"], settings)

        # Act
        candidates = collector.collect()

        # Assert — 失敗したカテゴリを無視して、成功したカテゴリの候補は返る
        assert any(c.url == "http://arxiv.org/abs/2401.00001v1" for c in candidates)

    def test_returns_empty_list_when_all_categories_fail(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.set_error("cs.SE", FetchError("取得に失敗しました"))
        collector = ArxivCollector(["cs.SE"], settings)

        # Act / Assert
        assert collector.collect() == ()


class TestFetchResourceUsage:
    def test_calls_fetch_resource_with_feed_content_types(
        self, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — SSRF ガード経路（fetch_resource）を必ず通ることを担保する
        fake_fetch_resource.set_response("cs.SE", _resource(ATOM_FEED))
        collector = ArxivCollector(["cs.SE"], settings)

        # Act
        collector.collect()

        # Assert
        assert len(fake_fetch_resource.calls) == 1
        call = fake_fetch_resource.calls[0]
        assert call["allowed_content_types"] == FEED_CONTENT_TYPES
