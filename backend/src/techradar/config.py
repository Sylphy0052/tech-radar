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
    # 空ならセッション既定のモデルを使う。再現性が要るときに固定する。
    claude_cli_model: str | None = None
    llm_timeout_seconds: int = Field(default=180, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)
    # 指数バックオフの基準秒数（n 回目の待機は base * 2^n）。
    llm_retry_backoff_seconds: float = Field(default=1.0, ge=0)

    # ---- Embedding (ローカル実行) ----
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    # HuggingFace のリビジョン。既定ブランチが差し替えられても影響を受けないよう固定する。
    # 空ならリビジョンを固定しない（開発時のみ）。
    embedding_model_revision: str | None = None
    # トークナイズ前に本文を切る文字数。max_seq_length はトークナイズ後に効くため、
    # 巨大な本文をそのまま渡すとトークナイズ自体で CPU とメモリを消費する。
    embedding_max_input_characters: int = Field(default=40000, gt=0)
    embedding_dimensions: int = Field(default=1024, gt=0)
    embedding_max_length: int = Field(default=8192, gt=0)
    embedding_device: EmbeddingDevice = "auto"
    embedding_batch_size: int = Field(default=8, gt=0)

    # ---- 記事取得 (SSRF 対策の各種上限。PROJECT_SPEC.md §21) ----
    fetch_max_redirects: int = Field(default=5, ge=0)
    fetch_max_response_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    fetch_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    fetch_read_timeout_seconds: float = Field(default=20.0, gt=0)
    # リダイレクト追跡と本文読み取りを合わせた全体の上限。
    # ホップ単位のタイムアウトだけでは、少量ずつ送り続ける相手に接続を占有される。
    fetch_total_timeout_seconds: float = Field(default=60.0, gt=0)
    fetch_user_agent: str = "TechRadar/0.1 (+https://gitlab.heroz.co.jp/kfuruhashi/techradar)"

    # ---- 記事収集 ----
    # 未設定なら Brave Search コレクターは実行時に skip される
    brave_search_api_key: str | None = None
    github_token: str | None = None

    # ---- アプリケーション ----
    recommendation_max_age_days: int = Field(default=7, gt=0)
    log_retention_days: int = Field(default=90, gt=0)
    worker_concurrency: int = Field(default=2, gt=0)
    # False ならジョブワーカーを起動しない。テストのたびに実ワーカーが DB を
    # ポーリングすると、テストが不安定になりテスト用 DB のトランザクションとも
    # 干渉するため、テスト側の既定は無効にする（`tests/conftest.py` を参照）。
    worker_enabled: bool = True
    # pending なジョブが無いときにワーカーが再試行までに待つ秒数。
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    # シャットダウン時に実行中ジョブの完了を待つ上限秒数。超過分はキャンセルして pending へ戻す。
    worker_shutdown_grace_seconds: float = Field(default=10.0, ge=0)
    # task.cancel() 後、キャンセル完了を待つ上限秒数。ハンドラが CancelledError を
    # 握り潰す実装だと際限なくブロックしうるため安全弁として設ける。超過した場合、
    # 該当ジョブの DB 状態は reclaim_stale（起動時・シャットダウン時）に委ねる。
    worker_cancel_await_timeout_seconds: float = Field(default=5.0, gt=0)
    # 3 回目の失敗で failed にする。無限リトライで詰まったジョブを残さないため。
    job_max_attempts: int = Field(default=3, ge=1)
    # ジョブ再試行の指数バックオフの基準秒数（n 回目の待機は base * 2^(n-1)）。
    job_retry_backoff_seconds: float = Field(default=5.0, gt=0)

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
