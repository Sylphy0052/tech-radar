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


def test_rejects_non_positive_worker_concurrency():
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        Settings(_env_file=None, worker_concurrency=0)


def test_uses_seven_days_as_recommendation_age_limit_by_default():
    # Arrange / Act
    settings = Settings(_env_file=None)

    # Assert — 仕様の「直近 7 日以内」に対応する
    assert settings.recommendation_max_age_days == 7


def test_caches_settings_singleton():
    # Arrange
    get_settings.cache_clear()

    # Act
    first = get_settings()
    second = get_settings()

    # Assert
    assert first is second
