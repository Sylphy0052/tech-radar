"""Hacker News コレクターを検証する（Issue #9 T6）。

HTTP は必ずモックし、実通信は行わない。`techradar.collectors.hackernews.fetch_resource`
を差し替えて `FetchedResource` を返す・例外を送出させることで、SSRF ガードを持つ
実際の HTTP 層には触れずにコレクターのロジックだけを検証する。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from techradar.collectors import hackernews
from techradar.collectors.base import CollectorError
from techradar.collectors.config import FeedsConfig
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import JSON_CONTENT_TYPES, FetchedResource


def _resource(payload: Any) -> FetchedResource:
    """指定したペイロードを JSON として持つ `FetchedResource` を作る。"""
    return FetchedResource(
        final_url="https://hacker-news.firebaseio.com/v0/dummy",
        body=json.dumps(payload).encode("utf-8"),
        text="",
        content_type="application/json",
        status_code=200,
    )


def _raw_resource(body: bytes) -> FetchedResource:
    """壊れた JSON をそのまま body に持つ `FetchedResource` を作る。"""
    return FetchedResource(
        final_url="https://hacker-news.firebaseio.com/v0/dummy",
        body=body,
        text="",
        content_type="application/json",
        status_code=200,
    )


def _feeds_config(*, hacker_news_top_items: int = 2) -> FeedsConfig:
    return FeedsConfig(
        freshness_days=7,
        max_candidates_per_run=200,
        hacker_news_top_items=hacker_news_top_items,
    )


def _item(
    item_id: int,
    *,
    url: str | None = "https://example.com/a",
    title: str | None = "サンプル記事",
    time: int | None = 1_700_000_000,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": item_id}
    if url is not None:
        payload["url"] = url
    if title is not None:
        payload["title"] = title
    if time is not None:
        payload["time"] = time
    return payload


def _item_id_from_url(url: str) -> int:
    return int(url.rsplit("/", 1)[-1].removesuffix(".json"))


@pytest.fixture
def calls() -> list[dict[str, Any]]:
    return []


def _install_fake_fetch(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
    responder: Callable[[str], FetchedResource],
) -> None:
    def fake_fetch_resource(
        url: str, *, allowed_content_types: tuple[str, ...], settings: object = None
    ) -> FetchedResource:
        calls.append({"url": url, "allowed_content_types": allowed_content_types})
        return responder(url)

    monkeypatch.setattr(hackernews, "fetch_resource", fake_fetch_resource)


class TestCollect:
    def test_fetches_only_the_configured_top_item_count(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — topstories には 10 件あるが設定値は 3 件
        topstories = list(range(1, 11))

        def responder(url: str) -> FetchedResource:
            if url == hackernews.TOP_STORIES_URL:
                return _resource(topstories)
            return _resource(_item(_item_id_from_url(url)))

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(
            feeds_config=_feeds_config(hacker_news_top_items=3)
        )

        # Act
        candidates = collector.collect()

        # Assert
        item_calls = [c for c in calls if c["url"] != hackernews.TOP_STORIES_URL]
        assert len(item_calls) == 3
        assert len(candidates) == 3

    def test_skips_items_without_a_url(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — Ask HN のような self post は url を持たない
        def responder(url: str) -> FetchedResource:
            if url == hackernews.TOP_STORIES_URL:
                return _resource([1, 2])
            item_id = _item_id_from_url(url)
            if item_id == 1:
                return _resource(_item(1, url=None))
            return _resource(_item(2))

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(
            feeds_config=_feeds_config(hacker_news_top_items=2)
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 1
        assert candidates[0].url == "https://example.com/a"

    def test_converts_unix_time_to_utc_published_at(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            if url == hackernews.TOP_STORIES_URL:
                return _resource([1])
            return _resource(_item(1, time=1_700_000_000))

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(
            feeds_config=_feeds_config(hacker_news_top_items=1)
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert candidates[0].published_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
        assert candidates[0].published_at is not None
        assert candidates[0].published_at.tzinfo is not None

    def test_one_item_failure_does_not_drop_other_candidates(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            if url == hackernews.TOP_STORIES_URL:
                return _resource([1, 2])
            item_id = _item_id_from_url(url)
            if item_id == 1:
                raise FetchError("boom")
            return _resource(_item(2))

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(
            feeds_config=_feeds_config(hacker_news_top_items=2)
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 1
        assert candidates[0].url == "https://example.com/a"

    def test_skips_items_with_malformed_json_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            if url == hackernews.TOP_STORIES_URL:
                return _resource([1, 2])
            item_id = _item_id_from_url(url)
            if item_id == 1:
                return _raw_resource(b"not json at all")
            return _resource(_item(2))

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(
            feeds_config=_feeds_config(hacker_news_top_items=2)
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 1

    def test_returns_empty_when_topstories_response_is_not_a_list(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — キー欠落・型違いの壊れた応答でも例外にしない
        def responder(url: str) -> FetchedResource:
            return _resource({"unexpected": "shape"})

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(feeds_config=_feeds_config())

        # Act
        candidates = collector.collect()

        # Assert
        assert candidates == []

    def test_raises_collector_error_when_topstories_request_fails(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            raise FetchError("network down")

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(feeds_config=_feeds_config())

        # Act / Assert
        with pytest.raises(CollectorError):
            collector.collect()

    def test_calls_fetch_resource_with_json_content_types(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — SSRF ガードを通る安全経路が使われていることの担保
        def responder(url: str) -> FetchedResource:
            if url == hackernews.TOP_STORIES_URL:
                return _resource([1])
            return _resource(_item(1))

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = hackernews.HackerNewsCollector(
            feeds_config=_feeds_config(hacker_news_top_items=1)
        )

        # Act
        collector.collect()

        # Assert
        assert calls
        assert all(c["allowed_content_types"] == JSON_CONTENT_TYPES for c in calls)
