"""DB エンジンとセッションの管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from techradar.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """エンジンのシングルトンを返す。

    プロセス内で接続プールを 1 つに保つ。テストで差し替える場合は
    `get_engine.cache_clear()` を呼ぶ。
    """
    settings = get_settings()
    return create_engine(str(settings.database_url), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    """セッションファクトリのシングルトンを返す。"""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """トランザクション境界を持つセッションを提供する。

    例外は握りつぶさず、ロールバックしたうえで呼び出し側へ再送出する。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
