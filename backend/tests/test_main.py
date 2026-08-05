"""アプリケーションのヘルスチェックと lifespan（ジョブワーカー起動）を検証する。"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from techradar import __version__
from techradar.config import Settings
from techradar.jobs.registry import JobHandlerRegistry
from techradar.main import create_app


def test_health_returns_ok_with_version():
    # Arrange
    app = create_app(Settings(_env_file=None))
    client = TestClient(app)

    # Act
    response = client.get("/api/health")

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "brave_search_enabled": False,
    }


def test_health_reports_brave_search_enabled_when_key_configured():
    # Arrange
    app = create_app(Settings(_env_file=None, brave_search_api_key="dummy-key"))
    client = TestClient(app)

    # Act
    response = client.get("/api/health")

    # Assert
    assert response.json()["brave_search_enabled"] is True


def test_keeps_injected_settings_after_lifespan_startup():
    # Arrange — TestClient を context manager として使うと lifespan が実行される
    app = create_app(Settings(_env_file=None, brave_search_api_key="dummy-key"))

    # Act
    with TestClient(app) as client:
        response = client.get("/api/health")

    # Assert — 起動処理が注入した設定を上書きしないこと
    assert response.json()["brave_search_enabled"] is True


def test_allows_cors_preflight_from_a_configured_origin():
    # Arrange
    app = create_app(Settings(_env_file=None, cors_allow_origins="http://localhost:19999"))
    client = TestClient(app)

    # Act
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:19999",
            "Access-Control-Request-Method": "GET",
        },
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:19999"


def test_rejects_cors_preflight_from_an_unconfigured_origin():
    # Arrange
    app = create_app(Settings(_env_file=None, cors_allow_origins="http://localhost:19999"))
    client = TestClient(app)

    # Act
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    # Assert — 許可していないオリジンには Allow-Origin を返さないこと
    assert "access-control-allow-origin" not in response.headers


def test_does_not_start_the_job_worker_when_worker_enabled_is_false():
    # Arrange — 実ワーカーが DB をポーリングし始めるとテストが不安定になるため、
    # 無効化した場合に lifespan を通してもワーカーが起動しないことを確認する。
    app = create_app(Settings(_env_file=None, worker_enabled=False))

    # Act
    with TestClient(app):
        pass

    # Assert
    assert app.state.job_worker is None


class _StubJobWorker:
    """`JobWorker` の代わりに使う、DB へ触れないスタブ。

    実ワーカーは DB をポーリングするため、`start`/`stop` が呼ばれたことだけを
    記録し、テストがタイムアウトしたり DB と干渉したりしないようにする。
    """

    instances: ClassVar[list[_StubJobWorker]] = []

    def __init__(self, *, settings: Settings, registry: JobHandlerRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.started = False
        self.stopped = False
        _StubJobWorker.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def test_starts_and_stops_the_job_worker_when_worker_enabled_is_true(
    monkeypatch: pytest.MonkeyPatch,
):
    # Arrange
    _StubJobWorker.instances = []
    monkeypatch.setattr("techradar.main.JobWorker", _StubJobWorker)
    app = create_app(Settings(_env_file=None, worker_enabled=True))

    # Act
    with TestClient(app):
        # Assert — 起動処理の中で start() 済みであること
        assert len(_StubJobWorker.instances) == 1
        worker: Any = _StubJobWorker.instances[0]
        assert worker.started is True
        assert worker.stopped is False

    # Assert — コンテキスト終了（lifespan のシャットダウン）で stop() 済みであること
    assert worker.stopped is True
