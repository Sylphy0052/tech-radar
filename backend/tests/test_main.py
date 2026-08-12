"""アプリケーションのヘルスチェックと lifespan（ジョブワーカー起動）を検証する。"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from techradar import __version__
from techradar import main as main_module
from techradar.config import Settings
from techradar.embedding.health import EmbeddingHealthCheckResult
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
    # Arrange — Embedding 実行環境の検査（Issue #78）は worker_enabled と同じ場所で
    # 実行される。実物の torch / sentence_transformers を読み込むと初回 import だけで
    # 数十秒かかる（実測）ため、このテストの本題（ワーカーの起動・停止）とは無関係な
    # コストを避けてスタブへ差し替える。
    _StubJobWorker.instances = []
    monkeypatch.setattr("techradar.main.JobWorker", _StubJobWorker)
    monkeypatch.setattr(
        main_module,
        "check_embedding_health",
        lambda *_a, **_k: EmbeddingHealthCheckResult(ok=True, device="cpu"),
    )
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


class TestEmbeddingHealthCheckInLifespan:
    """起動時の Embedding 実行環境検査を固定する（Issue #78）。

    2026-08-12、venv のインストールが不完全なまま起動し `embed_article`
    ジョブ 194 件が全滅した。検査に失敗しても、また検査関数自体が想定外の
    例外を投げても、記事登録やフィード表示に使うアプリの起動は続くことを
    固定する。ログの検証は `caplog` ではなく `techradar.main.logger` を直接
    差し替える。並列実行のワーカーでは `caplog` がハンドラを拾えず、実装が
    正しくても落ちることがある（`test_llm_managed_policy.py` / `test_jobs_worker.py`
    と同じ理由）。
    """

    def _create_app_with_stub_worker(
        self, monkeypatch: pytest.MonkeyPatch, **settings_kwargs: Any
    ) -> Any:
        _StubJobWorker.instances = []
        monkeypatch.setattr("techradar.main.JobWorker", _StubJobWorker)
        return create_app(Settings(_env_file=None, worker_enabled=True, **settings_kwargs))

    def test_検査に失敗しても起動が続く(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            main_module,
            "check_embedding_health",
            lambda *_a, **_k: EmbeddingHealthCheckResult(
                ok=False,
                error_type="ModuleNotFoundError",
                error_message="No module named 'torch'",
            ),
        )
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act
        with TestClient(app) as client:
            response = client.get("/api/health")

        # Assert — 検査が失敗してもヘルスチェックには応答し続ける
        assert response.status_code == 200

    def test_検査関数が想定外の例外を投げても起動が続く(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        def _raise(*_args: object, **_kwargs: object) -> EmbeddingHealthCheckResult:
            message = "unexpected failure"
            raise RuntimeError(message)

        monkeypatch.setattr(main_module, "check_embedding_health", _raise)
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act
        with TestClient(app) as client:
            response = client.get("/api/health")

        # Assert
        assert response.status_code == 200

    def test_成功時はデバイスを含むINFOログが出る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            main_module,
            "check_embedding_health",
            lambda *_a, **_k: EmbeddingHealthCheckResult(ok=True, device="xpu"),
        )
        info_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            main_module.logger, "info", lambda *args, **_kwargs: info_calls.append(args)
        )
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act
        with TestClient(app):
            pass

        # Assert
        assert any("xpu" in call for call in info_calls)

    def test_失敗時は原因を含むERRORログが出る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            main_module,
            "check_embedding_health",
            lambda *_a, **_k: EmbeddingHealthCheckResult(
                ok=False,
                error_type="ModuleNotFoundError",
                error_message="No module named 'torch'",
            ),
        )
        error_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            main_module.logger, "error", lambda *args, **_kwargs: error_calls.append(args)
        )
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act
        with TestClient(app):
            pass

        # Assert — 例外の型とメッセージが読み取れること
        assert any(
            "ModuleNotFoundError" in call and "No module named 'torch'" in call
            for call in error_calls
        )
