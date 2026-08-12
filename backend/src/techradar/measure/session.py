"""計測用の読み取り専用セッション（Issue #74）。

計測は本番 DB を参照する。書き込まないことをコード側の規律だけに委ねると、将来の
変更で崩れても気付けない。トランザクションを読み取り専用にして DB 側に拒否させる。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, SessionTransaction

from techradar.db.session import get_session_factory


def make_read_only(session: Session) -> None:
    """このセッションの現在のトランザクションを読み取り専用にする。

    `SET TRANSACTION READ ONLY` は現在のトランザクションに対して働く。書き込みが
    走った後では手遅れなので、セッションを使い始める前に呼ぶ。二重に呼んでも失敗しない
    （分離レベルの変更と違い、読み書きモードの変更に「最初の問い合わせより前」という
    制約は無い）。

    この関数が効くのは「今のトランザクション」だけである。ロールバック後に同じ
    セッションを使い続ける経路では、新しく始まったトランザクションは読み取り専用に
    ならない。呼び出し側の規律に依存させないため、通常は `enforce_read_only` を使う。
    """
    session.execute(text("SET TRANSACTION READ ONLY"))


def enforce_read_only(session: Session) -> None:
    """このセッションのトランザクションを、開始のたびに読み取り専用にする。

    `make_read_only` を 1 回呼ぶだけでは、ロールバック後に始まる次のトランザクションが
    書き込み可能なまま残る。計測が本番 DB を触る以上、担保は呼び出し方に依存させない。
    トランザクション開始のフックで毎回発行する。

    フックの中では `session.execute` を使わない。トランザクション開始の途中で
    セッション経由の問い合わせを行うと再入になるため、渡された接続へ直接発行する。
    """

    @event.listens_for(session, "after_begin")
    def _apply_read_only(
        _session: Session, _transaction: SessionTransaction, connection: Connection
    ) -> None:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    # 既に始まっているトランザクションにはフックが掛からないため、ここでも発行する。
    make_read_only(session)


@contextmanager
def read_only_session() -> Iterator[Session]:
    """読み取り専用のセッションを開く。

    `commit` はしない。計測は読むだけであり、書き込みは DB 側で拒否される。
    """
    session_factory = get_session_factory()
    with session_factory() as session:
        enforce_read_only(session)
        yield session
