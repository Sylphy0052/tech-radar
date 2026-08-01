"""FastAPI アプリケーションのエントリポイント。

ワーカーは常駐プロセスを増やさないため、`lifespan` でこのプロセスに同居させる。
常駐するのは PostgreSQL コンテナのみに保つ（`run.sh` は変更しない）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from techradar import __version__
from techradar.api.articles import router as articles_router
from techradar.api.crawl import router as crawl_router
from techradar.api.jobs import router as jobs_router
from techradar.api.sources import router as sources_router
from techradar.config import Settings, get_settings
from techradar.jobs.registry import create_default_registry
from techradar.jobs.worker import JobWorker

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """ヘルスチェックのレスポンス。"""

    status: str
    version: str
    brave_search_enabled: bool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動・終了時の処理。

    起動時に設定を読み込んで検証し、以降のリクエストで例外が出ないようにする。
    Embedding モデルのロード（Issue #6）もここに追加する。

    `create_app` が設定を注入済みの場合は上書きしない（テストから差し替えた
    設定が起動処理で握り潰されるのを防ぐ）。

    ジョブワーカーは `worker_enabled` が真のときだけ起動する。テストのたびに
    実ワーカーが DB をポーリングすると、テストが不安定になりテスト用 DB の
    トランザクションとも干渉するため、テスト側の既定は無効にする
    （`tests/conftest.py` 等）。ワーカーは PostgreSQL コンテナ以外の常駐プロセスを
    増やさないため、この FastAPI プロセスに同居させる（`run.sh` は変更しない）。
    """
    if getattr(app.state, "settings", None) is None:
        app.state.settings = get_settings()

    settings: Settings = app.state.settings
    worker: JobWorker | None = None
    if settings.worker_enabled:
        registry = create_default_registry(settings)
        worker = JobWorker(settings=settings, registry=registry)
        await worker.start()
    app.state.job_worker = worker

    try:
        yield
    finally:
        if worker is not None:
            try:
                await worker.stop()
            except Exception:
                logger.exception("lifespan.worker_stop_failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    """FastAPI アプリケーションを生成する。

    テストから設定を差し替えられるよう、ファクトリ関数として定義する。
    """
    resolved = settings or get_settings()
    app = FastAPI(
        title="TechRadar API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved

    # フロントエンド（Next.js dev server）からの呼び出しを許可する。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sources_router)
    app.include_router(jobs_router)
    app.include_router(crawl_router)
    app.include_router(articles_router)

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """アプリケーションの稼働状態を返す。"""
        current: Settings = app.state.settings
        return HealthResponse(
            status="ok",
            version=__version__,
            brave_search_enabled=current.is_brave_search_enabled,
        )

    return app


app = create_app()
