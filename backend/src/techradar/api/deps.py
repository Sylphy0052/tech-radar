"""API 層の共通依存（`PROJECT_SPEC.md` §25）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

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


def get_app_settings(request: Request) -> Settings:
    """リクエストが属するアプリケーションの `Settings` を返す。

    `create_app` が注入した設定（`request.app.state.settings`）を経由するため、
    テストから `create_app(Settings(...))` で差し替えた値がそのまま反映される。
    """
    settings: Settings = request.app.state.settings
    return settings


def get_current_user_id(request: Request) -> uuid.UUID:
    """現在のユーザー ID を返す。

    MVP は認証なしの単一ユーザー（`docs/decisions.md`）。`Settings.default_user_id`
    を返すだけの実装にしておき、将来認証を導入する際はこの依存の中身を
    差し替えるだけで済むようにする。
    """
    return get_app_settings(request).default_user_id


def get_now() -> datetime:
    """現在時刻（UTC）を返す。

    `datetime.now(UTC)` を直接呼ぶ代わりにこの依存を経由することで、テストから
    `app.dependency_overrides[get_now]` で固定時刻に差し替えられるようにする
    （`GET /api/feed` の run 再利用期限判定など、時刻に依存する挙動の検証に使う）。
    """
    return datetime.now(UTC)
