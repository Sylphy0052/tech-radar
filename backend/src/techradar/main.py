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
from techradar.api.feedback import router as feedback_router
from techradar.api.interests import router as interests_router
from techradar.api.jobs import router as jobs_router
from techradar.api.rate_limit import create_recommendation_rate_limiter
from techradar.api.recommendations import router as recommendations_router
from techradar.api.sources import router as sources_router
from techradar.config import Settings, get_settings
from techradar.embedding.health import check_embedding_health
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

    Embedding 実行環境の検査（`_check_embedding_health`、Issue #78）もワーカーと
    同じ条件で行う。`embed_article` ジョブはワーカーが動いていなければ実行されない
    ため、ワーカーを起動しないとき（テスト等）に検査しても意味が薄く、`torch` /
    `sentence_transformers` の import コストをテストのたびに払うことになる。
    """
    if getattr(app.state, "settings", None) is None:
        app.state.settings = get_settings()

    settings: Settings = app.state.settings
    worker: JobWorker | None = None
    if settings.worker_enabled:
        _check_embedding_health(settings)
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


def _check_embedding_health(settings: Settings) -> None:
    """Embedding 実行環境を検査し、結果をログに出す（Issue #78）。

    2026-08-12、venv のインストールが不完全なまま起動し、`embed_article`
    ジョブ 194 件が同じ理由で全滅した。起動時に検査してログへ出しておけば、
    ジョブが積み上がる前に気付けたはずだった。

    検査は補助であり、これが原因でアプリが起動しなくなるのは本末転倒のため、
    `check_embedding_health` が想定外の例外を送出した場合も含めて起動を止めない。
    """
    try:
        result = check_embedding_health(settings.embedding_device)
    except Exception:
        logger.exception("lifespan.embedding_health_check_raised")
        return

    if result.ok:
        logger.info("lifespan.embedding_health_check_ok device=%s", result.device)
    else:
        logger.error(
            "lifespan.embedding_health_check_failed error_type=%s error_message=%s "
            "embed_article ジョブは全て失敗する見込みです",
            result.error_type,
            result.error_message,
        )


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
    # 推薦 API のレート制限器（`api/rate_limit.py`）はアプリ単位で 1 つ保持する。
    # `settings` と同じくここで app.state に注入することで、`create_app` を
    # 都度呼ぶテストごとに独立したインスタンスになり、リクエスト回数の
    # カウントがテスト間で漏れない。
    app.state.recommendation_rate_limiter = create_recommendation_rate_limiter(resolved)

    # フロントエンド（Next.js dev server）からの呼び出しを許可する。許可する
    # オリジンは設定値（`Settings.cors_allow_origins`）から取る。frontend の
    # ポートは `.env` の `FRONTEND_PORT` で変えられるため、ここを固定にすると
    # ポートを変えた途端に preflight で弾かれる。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sources_router)
    app.include_router(jobs_router)
    app.include_router(crawl_router)
    app.include_router(articles_router)
    app.include_router(feedback_router)
    app.include_router(interests_router)
    app.include_router(recommendations_router)

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
