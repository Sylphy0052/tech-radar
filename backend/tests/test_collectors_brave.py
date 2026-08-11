"""`BraveSearchCollector` の巡回・パース・レート制御を検証する。"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from techradar.collectors import brave as brave_module
from techradar.collectors.brave import (
    MAX_QUERIES_PER_COLLECT,
    MIN_REQUEST_INTERVAL_SECONDS,
    BraveSearchCollector,
)
from techradar.config import Settings
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import JSON_CONTENT_TYPES, FetchedResource
from tests.test_fetcher_ssrf import fake_getaddrinfo


def _resource(payload: str, *, content_type: str = "application/json") -> FetchedResource:
    return FetchedResource(
        final_url="https://api.search.brave.com/res/v1/web/search",
        body=payload.encode("utf-8"),
        text=payload,
        content_type=content_type,
        status_code=200,
    )


WEB_RESULTS_PAYLOAD = """{
  "web": {
    "results": [
      {
        "url": "https://example.com/articles/1",
        "title": "Sample Article",
        "page_age": "2024-01-01T00:00:00Z"
      },
      {
        "url": "https://example.com/articles/2",
        "title": "No Page Age Article"
      }
    ]
  }
}"""


@pytest.fixture
def enabled_settings() -> Settings:
    return Settings(_env_file=None, brave_search_api_key="test-api-key")


@pytest.fixture
def disabled_settings() -> Settings:
    return Settings(_env_file=None, brave_search_api_key=None)


class _FakeFetchResource:
    """テストごとに呼び出し引数を記録し、登録順に応答を返すスタブ。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses: list[object] = []

    def queue_response(self, resource: FetchedResource) -> None:
        self._responses.append(resource)

    def queue_error(self, error: Exception) -> None:
        self._responses.append(error)

    def __call__(
        self,
        url: str,
        *,
        allowed_content_types: tuple[str, ...],
        settings: Settings | None = None,
        **extra: Any,
    ) -> FetchedResource:
        self.calls.append(
            {
                "url": url,
                "allowed_content_types": allowed_content_types,
                "headers": extra.get("headers"),
            }
        )
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FetchedResource)
        return result


@pytest.fixture
def fake_fetch_resource(monkeypatch: pytest.MonkeyPatch) -> _FakeFetchResource:
    fake = _FakeFetchResource()
    monkeypatch.setattr(brave_module, "fetch_resource", fake)
    return fake


class _FakeClock:
    """`sleep` が呼ばれるたびに `monotonic` の戻り値を進める偽時計。

    実時間を待たずにレート制御のロジック（前回リクエストからの経過時間）を
    検証するためのテスト用ダブル。
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class TestApiKeyMissing:
    def test_returns_empty_list_without_calling_fetch_resource(
        self, disabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        collector = BraveSearchCollector(["python"], disabled_settings)

        # Act
        candidates = collector.collect()

        # Assert — 例外を出さず空リスト。fetch_resource は一度も呼ばれない
        assert candidates == ()
        assert fake_fetch_resource.calls == []


class TestRateLimiting:
    def test_first_request_does_not_wait(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        clock = _FakeClock()
        collector = BraveSearchCollector(
            ["python"], enabled_settings, sleep=clock.sleep, monotonic=clock.monotonic
        )

        # Act
        collector.collect()

        # Assert — 1回目は待たない
        assert clock.sleep_calls == []

    def test_consecutive_requests_wait_at_least_the_minimum_interval(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — 2クエリ連続。1回目は即時応答、間隔を空けずに2回目が呼ばれる想定
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        clock = _FakeClock()
        collector = BraveSearchCollector(
            ["python", "rust"], enabled_settings, sleep=clock.sleep, monotonic=clock.monotonic
        )

        # Act
        collector.collect()

        # Assert — 2回目の前に最低 MIN_REQUEST_INTERVAL_SECONDS 待っている
        assert len(clock.sleep_calls) == 1
        assert clock.sleep_calls[0] >= MIN_REQUEST_INTERVAL_SECONDS

    def test_does_not_wait_when_enough_time_already_passed(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — 1回目のリクエスト後、待機なしで十分な時間が経過した体で2回目を呼ぶ
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        clock = _FakeClock()
        collector = BraveSearchCollector(
            ["python"], enabled_settings, sleep=clock.sleep, monotonic=clock.monotonic
        )
        collector.collect()
        clock.now += MIN_REQUEST_INTERVAL_SECONDS * 2

        # Act
        collector._collect_query("rust")

        # Assert — 間隔は十分空いているため待機しない
        assert clock.sleep_calls == []


class TestQueryLimit:
    def test_truncates_queries_to_the_max_per_collect(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — 上限を超えるクエリ数を渡す
        queries = [f"query-{i}" for i in range(MAX_QUERIES_PER_COLLECT + 5)]
        for _ in range(MAX_QUERIES_PER_COLLECT):
            fake_fetch_resource.queue_response(_resource('{"web": {"results": []}}'))
        clock = _FakeClock()
        collector = BraveSearchCollector(
            queries, enabled_settings, sleep=clock.sleep, monotonic=clock.monotonic
        )

        # Act
        collector.collect()

        # Assert — 投げたクエリ数が上限で打ち切られる
        assert len(fake_fetch_resource.calls) == MAX_QUERIES_PER_COLLECT


class TestResponseParsing:
    def test_returns_candidates_with_url_and_title(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act
        candidates = collector.collect()

        # Assert
        matched = next(c for c in candidates if c.url == "https://example.com/articles/1")
        assert matched.title == "Sample Article"
        assert matched.collector_name == "brave_search"
        assert matched.source_hint == "python"

    def test_missing_page_age_results_in_none_published_at(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act
        candidates = collector.collect()

        # Assert
        matched = next(c for c in candidates if c.url == "https://example.com/articles/2")
        assert matched.published_at is None

    def test_present_page_age_is_parsed_as_utc(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act
        candidates = collector.collect()

        # Assert
        matched = next(c for c in candidates if c.url == "https://example.com/articles/1")
        assert matched.published_at == datetime(2024, 1, 1, tzinfo=UTC)

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            "{}",
            '{"web": "not-a-dict"}',
            '{"web": {"results": "not-a-list"}}',
            '{"web": {"results": [1, 2, 3]}}',
            '{"web": {"results": [{"title": "no url"}]}}',
            '{"web": {"results": [{"url": "https://example.com/x"}]}}',
        ],
    )
    def test_broken_or_unexpected_json_structure_is_skipped_without_raising(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource, payload: str
    ):
        # Arrange
        fake_fetch_resource.queue_response(_resource(payload))
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act
        candidates = collector.collect()

        # Assert — 例外を出さずスキップする
        assert candidates == ()


class TestCollectResilience:
    def test_continues_when_one_query_fetch_fails(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — 1クエリ目は取得失敗、2クエリ目は成功
        fake_fetch_resource.queue_error(FetchError("取得に失敗しました"))
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        clock = _FakeClock()
        collector = BraveSearchCollector(
            ["broken", "healthy"], enabled_settings, sleep=clock.sleep, monotonic=clock.monotonic
        )

        # Act
        candidates = collector.collect()

        # Assert — 失敗したクエリを無視して、成功したクエリの候補は返る
        assert any(c.url == "https://example.com/articles/1" for c in candidates)


class TestFetchResourceUsage:
    def test_calls_fetch_resource_with_json_content_types(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — SSRF ガード経路（fetch_resource）を必ず通ることを担保する
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act
        collector.collect()

        # Assert
        assert len(fake_fetch_resource.calls) == 1
        call = fake_fetch_resource.calls[0]
        assert call["allowed_content_types"] == JSON_CONTENT_TYPES

    def test_calls_fetch_resource_with_subscription_token_header_via_normal_kwarg(
        self, enabled_settings: Settings, fake_fetch_resource: _FakeFetchResource
    ):
        # Arrange — `**kwargs` 展開の暫定策を除去し、通常のキーワード引数で
        # 呼び出していることを確かめる（前任者の回避策の再発防止）。
        fake_fetch_resource.queue_response(_resource(WEB_RESULTS_PAYLOAD))
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act
        collector.collect()

        # Assert
        call = fake_fetch_resource.calls[0]
        assert call["headers"] == {"X-Subscription-Token": "test-api-key"}


class TestFetchResourceIntegration:
    """`fetch_resource` をスタブ化しない結合的なテスト。

    `**kwargs` 展開の暫定策は型チェックが通っても実行時に `TypeError` になる
    バグだった。`fetch_resource` をスタブ化した通常のテストではこの種の
    呼び出しシグネチャ不一致を検出できないため、ここでは httpx の
    トランスポート層だけをモックし、`BraveSearchCollector` が実際の
    `fetch_resource` を最後まで呼び出せることを担保する。
    """

    def test_collect_sends_subscription_token_header_via_real_fetch_resource(
        self, enabled_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"web": {"results": []}}',
            )

        class _Client(httpx.Client):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(fetcher_http.httpx, "Client", _Client)
        collector = BraveSearchCollector(["python"], enabled_settings)

        # Act — fetch_resource はスタブ化しない。呼び出しシグネチャの不一致が
        # あれば TypeError としてここで検出される。
        collector.collect()

        # Assert
        assert seen["x-subscription-token"] == "test-api-key"
