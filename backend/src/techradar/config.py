"""アプリケーション設定。

設定はすべて環境変数から読み込む。値のハードコードはしない。
サンプルはリポジトリルートの `.env.example` を参照。
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# リポジトリルート (backend/src/techradar/config.py から 3 階層上)
REPO_ROOT = Path(__file__).resolve().parents[3]

EmbeddingDevice = Literal["auto", "cuda", "cpu", "xpu"]

# MVP は認証なしの単一ユーザー（`docs/decisions.md`）のため、固定 UUID を既定の
# user_id として使う。将来認証を導入する際は `api.deps.get_current_user_id` の
# 実装を差し替えるだけで済むようにするため、値そのものは環境変数
# `DEFAULT_USER_ID` で上書き可能にしておく。
_DEFAULT_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# frontend（Next.js dev server）の既定オリジン。`run.sh` の `FRONTEND_PORT` の
# 既定値と一致させる。よく使われる 3000 番台は他プロセスと衝突しやすいため、
# ephemeral port range（32768-60999）の外にある 5 桁を既定にする。
_DEFAULT_FRONTEND_ORIGIN = "http://localhost:13700"


def _reject_origin_that_cannot_match(origin: str) -> None:
    """ブラウザが送る `Origin` ヘッダと一致し得ない表記なら `ValueError` を送出する。

    `CORSMiddleware` は許可リストと `Origin` ヘッダを完全一致で比較する。表記が
    ずれていても起動自体は成功し、preflight だけが静かに落ちるため原因が追いにくい。
    起動時に弾いて設定した本人へ知らせる。

    ワイルドカードだけは事情が異なる。完全一致比較の下では単にマッチしないだけだが、
    `allow_origin_regex` を併用する変更が入った途端に本物のワイルドカードとして働く。
    `main.create_app` は `allow_credentials=True` を指定しているため、そのとき許可
    範囲が一気に広がる（Issue #62）。
    """
    # 生の文字列を先に見る。`urlsplit` は解析前にタブと改行を取り除くため、
    # 解析結果だけを検証すると制御文字入りの値が素通りして許可リストへ入る。
    # 非 ASCII も同様で、ブラウザは IDN を Punycode へ変換して送るため一致しない。
    if not origin.isascii() or not origin.isprintable() or " " in origin:
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' は ASCII の印字可能文字で指定してください"
            "（制御文字・空白・非 ASCII 文字は Origin ヘッダに現れないため一致しない）"
        )
        raise ValueError(message)
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' は "
            "http(s)://<host>[:<port>] の形式で指定してください"
        )
        raise ValueError(message)
    if parsed.path or parsed.query or parsed.fragment:
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' にパス・クエリは指定できません"
            "（Origin ヘッダと文字列一致しないため末尾スラッシュも不可）"
        )
        raise ValueError(message)
    # userinfo をワイルドカードより先に見る。逆順だとパスワードに `*` を含む値が
    # 「ワイルドカードは指定できません」と報告され、本当の原因が分からなくなる。
    if parsed.username is not None or parsed.password is not None:
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' に認証情報は指定できません"
            "（Origin ヘッダに userinfo は含まれないため一致しない）"
        )
        raise ValueError(message)
    if "*" in parsed.netloc:
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' にワイルドカードは指定できません"
            "（許可リストは Origin ヘッダとの完全一致で判定するため、書けてもマッチしない）"
        )
        raise ValueError(message)
    if not parsed.hostname:
        message = f"CORS_ALLOW_ORIGINS の値 '{origin}' からホスト名を読み取れません"
        raise ValueError(message)
    try:
        _ = parsed.port
    except ValueError as error:
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' のポート番号を読み取れません"
            "（ポートは 0-65535 の整数で指定する）"
        )
        raise ValueError(message) from error
    # 小文字へ正規化せず拒否する。正規化すると設定ミスに気付かないまま動いてしまい、
    # 後から表記を直す動機が消える。path・query・fragment は上で空を確認済みなので、
    # ここで比較しているのは実質スキームとホストだけになる。
    if origin != origin.lower():
        message = (
            f"CORS_ALLOW_ORIGINS の値 '{origin}' は小文字で指定してください"
            "（ブラウザはスキームとホストを小文字に正規化して Origin ヘッダを送るため、"
            "大文字のままでは一致しない）"
        )
        raise ValueError(message)


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
    # 管理者ポリシーが配布されたホストでも CLI を起動する。既定では起動しない。
    # ポリシー配下では CLI 側の隔離がほとんど機能しないため（`llm.managed_policy`）、
    # 中身を確認して無害だと判断できたときだけ真にする。
    allow_managed_policy: bool = False

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
    # MVP は認証なしの単一ユーザー（`docs/decisions.md`）。全レコードの user_id
    # にこの値を使う。`api.deps.get_current_user_id` から参照する。
    default_user_id: uuid.UUID = Field(default=_DEFAULT_USER_ID)
    # CORS を許可するオリジン。既定は `run.sh` が起動する frontend のみ。
    # ポートは既定値を変えられるため（`.env` の `FRONTEND_PORT`）、許可オリジンも
    # 環境変数で追随できるようにする。`allow_credentials=True` と併用するため
    # ワイルドカードは受け付けない（`main.create_app`）。
    # `NoDecode` を付けて pydantic-settings の自動 JSON デコードを止める。付けないと
    # 環境変数や設定ファイルから読んだ値が、下の `mode="before"` バリデータへ渡る前に
    # JSON として解釈され、カンマ区切りで書いた時点で `SettingsError` になる。この
    # デコードは失敗した瞬間に例外を投げるため、バリデータには到達しない（Issue #58）。
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [_DEFAULT_FRONTEND_ORIGIN]
    )
    log_retention_days: int = Field(default=90, gt=0)
    # `recommendation_runs` の保持期間（Issue #28）。`GET /api/feed` は呼ばれる
    # たびに run を作りうるため、`operation_logs` と同じ発想で古い run を削除する。
    # cursor ページングの途中で対象 run が消えると以降のページ要求が 400 になるが、
    # 既定の 30 日はページング所要時間より十分長いため実害はない。
    recommendation_run_retention_days: int = Field(default=30, gt=0)
    # 推薦 API のレート制限（Issue #28、`PROJECT_SPEC.md` §24）。
    # ウィンドウ内でこの回数を超えたら 429 を返す。
    recommendation_rate_limit_requests: int = Field(default=30, gt=0)
    recommendation_rate_limit_window_seconds: float = Field(default=60.0, gt=0)
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

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_comma_separated_origins(cls, value: object) -> object:
        """文字列で書かれたオリジンをリストとして解釈する。

        `list[str]` の設定を pydantic-settings に素直に渡すと JSON 形式
        （`["http://..."]`）でしか書けず、設定ファイルに書きづらい。区切り文字を
        許すことで `A,B` の形式でも設定できるようにする。空要素は除去する。

        JSON 形式も引き続き受け付ける。フィールドに `NoDecode` を付けて自動
        デコードを止めた以上、ここで解釈しないと従来 JSON で書いていた設定が
        1 要素の文字列として扱われてしまうため。
        """
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                # 壊れた JSON をカンマ区切りとして読み直しても意味のある結果に
                # ならないが、`_validate_origins` が表記を検証して弾く。
                pass
        return [origin.strip() for origin in value.split(",") if origin.strip()]

    @field_validator("cors_allow_origins", mode="after")
    @classmethod
    def _validate_origins(cls, value: list[str]) -> list[str]:
        """空リストと、オリジンとして成立しない表記を拒否する。

        空にすると全オリジンが拒否され、frontend から一切呼べない状態を設定ミスで
        作れてしまう。CORS を無効化したい意図と区別が付かないため、値を必須にする。

        個々の表記の検証は `_reject_origin_that_cannot_match` が担う。
        """
        if not value:
            message = "CORS_ALLOW_ORIGINS には 1 つ以上のオリジンを指定してください"
            raise ValueError(message)
        for origin in value:
            _reject_origin_that_cannot_match(origin)
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
