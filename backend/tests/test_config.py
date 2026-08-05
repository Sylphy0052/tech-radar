"""設定読み込みの振る舞いを検証する。"""

import pytest

from techradar.config import Settings, get_settings


def test_returns_default_embedding_dimensions_matching_pgvector_schema():
    # Arrange / Act
    settings = Settings(_env_file=None)

    # Assert — pgvector の vector(1024) と一致していること
    assert settings.embedding_dimensions == 1024


def test_treats_empty_brave_api_key_as_unset():
    # Arrange / Act
    settings = Settings(_env_file=None, brave_search_api_key="")

    # Assert
    assert settings.brave_search_api_key is None
    assert settings.is_brave_search_enabled is False


def test_enables_brave_search_when_api_key_is_present():
    # Arrange / Act
    settings = Settings(_env_file=None, brave_search_api_key="dummy-key")

    # Assert
    assert settings.is_brave_search_enabled is True


def test_allows_the_local_frontend_origin_by_default():
    # Arrange / Act
    settings = Settings(_env_file=None)

    # Assert — run.sh が既定で起動する frontend のオリジンと一致していること
    assert settings.cors_allow_origins == ["http://localhost:13700"]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://localhost:13700", ["http://localhost:13700"]),
        (
            "http://localhost:13700,http://127.0.0.1:13700",
            ["http://localhost:13700", "http://127.0.0.1:13700"],
        ),
        (
            " http://localhost:13700 , http://127.0.0.1:13700 ",
            ["http://localhost:13700", "http://127.0.0.1:13700"],
        ),
        ("http://localhost:13700,,", ["http://localhost:13700"]),
    ],
)
def test_splits_comma_separated_cors_allow_origins(configured: str, expected: list[str]):
    # Arrange / Act
    settings = Settings(_env_file=None, cors_allow_origins=configured)

    # Assert
    assert settings.cors_allow_origins == expected


def test_rejects_empty_cors_allow_origins():
    # Arrange / Act / Assert — 全オリジン拒否の設定を事故で作らないため
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_allow_origins="")


def test_rejects_non_positive_worker_concurrency():
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        Settings(_env_file=None, worker_concurrency=0)


def test_caches_settings_singleton():
    # Arrange
    get_settings.cache_clear()

    # Act
    first = get_settings()
    second = get_settings()

    # Assert
    assert first is second
