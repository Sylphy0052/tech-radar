"""GitHub Releases コレクターを検証する（Issue #9 T7）。

HTTP は必ずモックし、実通信は行わない。`techradar.collectors.github_releases.fetch_resource`
を差し替えて `FetchedResource` を返す・例外を送出させることで、SSRF ガードを持つ
実際の HTTP 層には触れずにコレクターのロジックだけを検証する。
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from techradar.collectors import github_releases
from techradar.collectors.config import FeedsConfig
from techradar.config import Settings
from techradar.fetcher import http as fetcher_http
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import JSON_CONTENT_TYPES, FetchedResource
from tests.test_fetcher_ssrf import fake_getaddrinfo


def _resource(payload: Any) -> FetchedResource:
    """指定したペイロードを JSON として持つ `FetchedResource` を作る。"""
    return FetchedResource(
        final_url="https://api.github.com/dummy",
        body=json.dumps(payload).encode("utf-8"),
        text="",
        content_type="application/json",
        status_code=200,
    )


def _release(
    *,
    html_url: str = "https://github.com/owner/repo/releases/tag/v1.0.0",
    name: str | None = "v1.0.0",
    tag_name: str = "v1.0.0",
    draft: bool = False,
    prerelease: bool = False,
    published_at: str | None = "2024-01-01T00:00:00Z",
    created_at: str | None = "2023-12-31T00:00:00Z",
) -> dict[str, Any]:
    release: dict[str, Any] = {
        "html_url": html_url,
        "tag_name": tag_name,
        "draft": draft,
        "prerelease": prerelease,
    }
    if name is not None:
        release["name"] = name
    if published_at is not None:
        release["published_at"] = published_at
    if created_at is not None:
        release["created_at"] = created_at
    return release


def _feeds_config(*, github_repositories: list[str]) -> FeedsConfig:
    return FeedsConfig(
        freshness_days=7,
        max_candidates_per_run=200,
        github_repositories=github_repositories,
        hacker_news_top_items=1,
    )


@pytest.fixture
def calls() -> list[dict[str, Any]]:
    return []


def _install_fake_fetch(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
    responder: Callable[[str], FetchedResource],
) -> None:
    def fake_fetch_resource(
        url: str,
        *,
        allowed_content_types: tuple[str, ...],
        headers: Any = None,
        settings: object = None,
    ) -> FetchedResource:
        calls.append(
            {"url": url, "allowed_content_types": allowed_content_types, "headers": headers}
        )
        return responder(url)

    monkeypatch.setattr(github_releases, "fetch_resource", fake_fetch_resource)


class TestCollect:
    def test_creates_candidates_with_html_url_and_published_at(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            return _resource([_release()])

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.url == "https://github.com/owner/repo/releases/tag/v1.0.0"
        assert candidate.published_at == datetime(2024, 1, 1, tzinfo=UTC)
        assert candidate.source_hint == "owner/repo"
        assert candidate.title == "owner/repo v1.0.0"

    def test_excludes_draft_and_prerelease(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            return _resource(
                [
                    _release(draft=True, tag_name="v0.1.0-draft"),
                    _release(prerelease=True, tag_name="v0.2.0-rc1"),
                    _release(tag_name="v1.0.0"),
                ]
            )

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        candidates = collector.collect()

        # Assert — 正式リリースのみが残る
        assert len(candidates) == 1
        assert candidates[0].title == "owner/repo v1.0.0"

    def test_falls_back_to_tag_name_when_name_is_empty(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            return _resource([_release(name="", tag_name="v3.2.1")])

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert candidates[0].title == "owner/repo v3.2.1"

    def test_falls_back_to_created_at_when_published_at_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            return _resource([_release(published_at=None, created_at="2023-06-15T12:00:00Z")])

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert candidates[0].published_at == datetime(2023, 6, 15, 12, 0, 0, tzinfo=UTC)

    def test_published_at_is_none_when_both_timestamps_are_missing(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — ここで現在時刻などで補完しない
        def responder(url: str) -> FetchedResource:
            return _resource([_release(published_at=None, created_at=None)])

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert candidates[0].published_at is None

    def test_one_repository_failure_does_not_drop_others(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            if "broken" in url:
                raise FetchError("boom")
            return _resource([_release()])

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/broken", "owner/ok"])
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert len(candidates) == 1
        assert candidates[0].source_hint == "owner/ok"

    def test_skips_repository_with_malformed_response_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — キー欠落・型違いの壊れた応答でも例外にしない
        def responder(url: str) -> FetchedResource:
            return _resource({"unexpected": "shape"})

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        candidates = collector.collect()

        # Assert
        assert candidates == []

    def test_calls_fetch_resource_with_json_content_types(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — SSRF ガードを通る安全経路が使われていることの担保
        def responder(url: str) -> FetchedResource:
            return _resource([_release()])

        _install_fake_fetch(monkeypatch, calls, responder)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"])
        )

        # Act
        collector.collect()

        # Assert
        assert calls
        assert all(c["allowed_content_types"] == JSON_CONTENT_TYPES for c in calls)


class TestAuthentication:
    """`Settings.github_token` に応じた `Authorization` ヘッダの付与を検証する（Issue #9 T16）。"""

    def test_adds_authorization_header_when_github_token_is_set(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange
        def responder(url: str) -> FetchedResource:
            return _resource([_release()])

        _install_fake_fetch(monkeypatch, calls, responder)
        settings = Settings(_env_file=None, github_token="test-github-token")  # noqa: S106
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"]), settings=settings
        )

        # Act
        collector.collect()

        # Assert
        assert calls[0]["headers"] == {"Authorization": "Bearer test-github-token"}

    def test_omits_authorization_header_when_github_token_is_unset(
        self, monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]
    ) -> None:
        # Arrange — 未設定でも従来どおり未認証で動作すること
        def responder(url: str) -> FetchedResource:
            return _resource([_release()])

        _install_fake_fetch(monkeypatch, calls, responder)
        settings = Settings(_env_file=None, github_token=None)
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"]), settings=settings
        )

        # Act
        collector.collect()

        # Assert
        assert calls[0]["headers"] == {}

    def test_token_value_does_not_appear_in_log_output_on_fetch_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        calls: list[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Arrange — 取得失敗時の警告ログにトークンが漏れないことを確認する
        def responder(url: str) -> FetchedResource:
            raise FetchError("boom")

        _install_fake_fetch(monkeypatch, calls, responder)
        settings = Settings(_env_file=None, github_token="super-secret-token")  # noqa: S106
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"]), settings=settings
        )

        # Act
        with caplog.at_level(logging.WARNING):
            collector.collect()

        # Assert
        assert "super-secret-token" not in caplog.text


class TestFetchResourceIntegration:
    """`fetch_resource` をスタブ化しない結合的なテスト。

    呼び出しシグネチャの不一致はスタブ化したテストでは検出できない
    （本タスクの起点になったバグそのもの）。ここでは httpx のトランスポート層
    だけをモックし、`GitHubReleasesCollector` が実際の `fetch_resource` を
    最後まで呼び出せることを担保する。
    """

    def test_collect_sends_authorization_header_via_real_fetch_resource(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.216.34"))

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"[]")

        class _Client(httpx.Client):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(fetcher_http.httpx, "Client", _Client)
        settings = Settings(_env_file=None, github_token="test-github-token")  # noqa: S106
        collector = github_releases.GitHubReleasesCollector(
            feeds_config=_feeds_config(github_repositories=["owner/repo"]), settings=settings
        )

        # Act — fetch_resource はスタブ化しない。呼び出しシグネチャの不一致が
        # あれば TypeError としてここで検出される。
        candidates = collector.collect()

        # Assert
        assert candidates == []
        assert seen["authorization"] == "Bearer test-github-token"
