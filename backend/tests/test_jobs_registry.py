"""`techradar.jobs.registry` の振る舞いテスト。"""

from __future__ import annotations

import uuid

import pytest

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


def test_create_default_registry_registers_only_crawl_sources() -> None:
    """`fetch_article` 等はまだ実装がないため、未登録種別として検出できるままにする。"""
    # Arrange / Act
    registry = create_default_registry()

    # Assert
    assert registry.registered_types == frozenset({JobType.CRAWL_SOURCES})
    for job_type in JobType:
        if job_type is JobType.CRAWL_SOURCES:
            assert registry.get(job_type) is not None
        else:
            assert registry.get(job_type) is None


async def test_create_default_registry_crawl_sources_handler_is_a_noop() -> None:
    """`crawl_sources` は Issue #9 が実装するまでのプレースホルダ（no-op）である。"""
    # Arrange
    registry = create_default_registry()
    handler = registry.get(JobType.CRAWL_SOURCES)
    assert handler is not None
    context = JobContext(
        job_id=uuid.uuid4(), job_type=JobType.CRAWL_SOURCES, payload={}, attempts=0
    )

    # Act / Assert: 例外を出さずに終了する
    assert await handler(context) is None
