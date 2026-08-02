"""`techradar.jobs.registry` の振る舞いテスト。"""

from __future__ import annotations

import pytest

from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.jobs.registry import JobContext, JobHandlerRegistry, create_default_registry


async def _noop_handler(context: JobContext) -> None:
    return None


def test_register_and_get_returns_the_registered_handler() -> None:
    # Arrange
    registry = JobHandlerRegistry()

    # Act
    registry.register(JobType.FETCH_ARTICLE, _noop_handler)

    # Assert
    assert registry.get(JobType.FETCH_ARTICLE) is _noop_handler
    assert registry.registered_types == frozenset({JobType.FETCH_ARTICLE})


def test_get_returns_none_for_an_unregistered_job_type() -> None:
    # Arrange
    registry = JobHandlerRegistry()

    # Act / Assert
    assert registry.get(JobType.FETCH_ARTICLE) is None
    assert registry.registered_types == frozenset()


def test_register_raises_value_error_on_duplicate_registration() -> None:
    """二重登録は実装漏れ・上書き事故に早期に気付けるよう ValueError にする（受入基準）。"""
    # Arrange
    registry = JobHandlerRegistry()
    registry.register(JobType.FETCH_ARTICLE, _noop_handler)

    # Act / Assert
    with pytest.raises(ValueError, match="fetch_article"):
        registry.register(JobType.FETCH_ARTICLE, _noop_handler)


def test_create_default_registry_registers_url_registration_and_crawl_handlers() -> None:
    """受入基準: URL 登録の end-to-end（Issue #12 T3）に必要な種別が登録される。

    `generate_feed` / `deduplicate_articles` はまだハンドラの実装が無い
    後続タスクの担当のため、未登録種別として検出できるままにする。
    """
    # Arrange / Act
    registry = create_default_registry(Settings(_env_file=None))

    # Assert
    registered = {
        JobType.CRAWL_SOURCES,
        JobType.FETCH_ARTICLE,
        JobType.ANALYZE_ARTICLE,
        JobType.EMBED_ARTICLE,
    }
    assert registry.registered_types == frozenset(registered)
    for job_type in JobType:
        if job_type in registered:
            assert registry.get(job_type) is not None
        else:
            assert registry.get(job_type) is None
