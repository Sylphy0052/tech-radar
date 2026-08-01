"""API 層の共通依存（`PROJECT_SPEC.md` §25）。"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db import session_scope


def get_session() -> Iterator[Session]:
    """リクエスト単位の DB セッションを提供する。

    正常終了時にコミットし、例外時はロールバックする（`session_scope` に委譲）。
    テストからは `app.dependency_overrides` で差し替える。
    """
    with session_scope() as session:
        yield session


def get_current_settings(request: Request) -> Settings:
    """このリクエストが属するアプリインスタンスの設定を返す。

    プロセス単位でキャッシュする `get_settings()` ではなく `app.state.settings`
    を参照する。`create_app(Settings(...))` でテストから差し替えた設定を
    ハンドラ側でも一貫して使うため（`main.py` の health エンドポイントと同じ方針）。
    """
    settings: Settings = request.app.state.settings
    return settings
