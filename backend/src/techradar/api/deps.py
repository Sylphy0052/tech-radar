"""API 層の共通依存（`PROJECT_SPEC.md` §25）。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from techradar.db import session_scope


def get_session() -> Iterator[Session]:
    """リクエスト単位の DB セッションを提供する。

    正常終了時にコミットし、例外時はロールバックする（`session_scope` に委譲）。
    テストからは `app.dependency_overrides` で差し替える。
    """
    with session_scope() as session:
        yield session
