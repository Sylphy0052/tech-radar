"""DB 例外の判定ヘルパー。

一意制約違反かどうかの判定は、API 層で「重複」を通常の応答へ変換する箇所ごとに
必要になる。同じ判定を各モジュールへ書き写すと、SQLSTATE の扱いが片方だけ
直されるといったずれが起きるため、ここへ集約する。
"""

from __future__ import annotations

from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

UNIQUE_VIOLATION_SQLSTATE = "23505"


def is_unique_violation(exc: IntegrityError) -> bool:
    """一意制約違反（SQLSTATE 23505）かどうかを判定する。

    psycopg の例外は `orig.sqlstate` に SQLSTATE を持つ。属性が無いドライバ・
    モック例外に備え、psycopg の例外クラスでも二重にフォールバック判定する。
    """
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate == UNIQUE_VIOLATION_SQLSTATE
    return isinstance(exc.orig, UniqueViolation)
