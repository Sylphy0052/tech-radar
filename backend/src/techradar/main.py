"""FastAPI アプリケーションのエントリポイント。

ワーカーは常駐プロセスを増やさないため、`lifespan` でこのプロセスに同居させる
（Issue #8 で実装）。常駐するのは PostgreSQL コンテナのみに保つ。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from techradar import __version__
from techradar.config import Settings, get_settings


class HealthResponse(BaseModel):
    """ヘルスチェックのレスポンス。"""

    status: str
    version: str
    brave_search_enabled: bool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動・終了時の処理。

    起動時に設定を読み込んで検証し、以降のリクエストで例外が出ないようにする。
    Embedding モデルのロード（Issue #6）とジョブワーカーの起動（Issue #8）も
    ここに追加する。

    `create_app` が設定を注入済みの場合は上書きしない（テストから差し替えた
    設定が起動処理で握り潰されるのを防ぐ）。
    """
    if getattr(app.state, "settings", None) is None:
        app.state.settings = get_settings()
    yield


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
