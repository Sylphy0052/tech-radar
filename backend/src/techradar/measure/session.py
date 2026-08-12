"""計測用の読み取り専用セッション（Issue #74）。

計測は本番 DB を参照する。書き込まないことをコード側の規律だけに委ねると、将来の
変更で崩れても気付けない。トランザクションを読み取り専用にして DB 側に拒否させる。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from techradar.db.session import get_session_factory


def make_read_only(session: Session) -> None:
    """このセッションの現在のトランザクションを読み取り専用にする。

    `SET TRANSACTION READ ONLY` はトランザクション内の最初の問い合わせより前に
    実行する必要がある。SQLAlchemy は最初の SQL 実行時にトランザクションを開始
    するため、セッションを使い始める前に呼ぶ。二重に呼んでも失敗しない。
    """
    session.execute(text("SET TRANSACTION READ ONLY"))


@contextmanager
def read_only_session() -> Iterator[Session]:
    """読み取り専用のセッションを開く。

    `commit` はしない。計測は読むだけであり、書き込みは DB 側で拒否される。
    """
    session_factory = get_session_factory()
    with session_factory() as session:
        make_read_only(session)
        yield session
