"""アプリケーション設定。

設定はすべて環境変数から読み込む。値のハードコードはしない。
サンプルはリポジトリルートの `.env.example` を参照。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# リポジトリルート (backend/src/techradar/config.py から 3 階層上)
REPO_ROOT = Path(__file__).resolve().parents[3]

EmbeddingDevice = Literal["auto", "cuda", "cpu"]


class Settings(BaseSettings):
    """環境変数から読み込むアプリケーション設定。"""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- データベース ----
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://techradar:techradar@localhost:5432/techradar"),
    )

    # ---- LLM (Claude Code CLI headless) ----
    claude_cli_path: str = "claude"
    llm_timeout_seconds: int = Field(default=180, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    # ---- Embedding (ローカル実行) ----
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dimensions: int = Field(default=1024, gt=0)
    embedding_max_length: int = Field(default=8192, gt=0)
    embedding_device: EmbeddingDevice = "auto"
    embedding_batch_size: int = Field(default=8, gt=0)

    # ---- 記事収集 ----
    # 未設定なら Brave Search コレクターは実行時に skip される
    brave_search_api_key: str | None = None
    github_token: str | None = None

    # ---- アプリケーション ----
    recommendation_max_age_days: int = Field(default=7, gt=0)
    log_retention_days: int = Field(default=90, gt=0)
    worker_concurrency: int = Field(default=2, gt=0)

    @field_validator("brave_search_api_key", "github_token", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value: object) -> object:
        """`.env` に `KEY=` と書かれた場合は未設定として扱う。"""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_brave_search_enabled(self) -> bool:
        """Brave Search コレクターを有効化できるか。"""
        return self.brave_search_api_key is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """設定のシングルトンを返す。

    プロセス内で 1 度だけ読み込み、以降はキャッシュを返す。
    テストで差し替える場合は `get_settings.cache_clear()` を呼ぶ。
    """
    return Settings()
