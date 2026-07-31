"""アプリケーションのヘルスチェックを検証する。"""

from fastapi.testclient import TestClient

from techradar import __version__
from techradar.config import Settings
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
