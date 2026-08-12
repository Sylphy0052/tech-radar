"""計測用セッションが読み取り専用であることのテスト（Issue #74）。

計測は本番 DB を参照する。書き込まない約束をコード側の規律にとどめると、将来の変更で
崩れても気付けない。DB のトランザクション属性で拒否させ、その挙動をここで固定する。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import InternalError
from sqlalchemy.orm import Session, sessionmaker

from techradar.db.models import Article
from techradar.measure.session import make_read_only, read_only_session


class TestMakeReadOnly:
    def test_allows_reading(self, migrated_engine: Engine) -> None:
        session_factory = sessionmaker(bind=migrated_engine)
        with session_factory() as session:
            make_read_only(session)

            assert session.execute(text("select 1")).scalar_one() == 1

    def test_rejects_insert(self, migrated_engine: Engine) -> None:
        """INSERT は DB 側で拒否される。計測が運用データを壊さないことの担保。"""
        session_factory = sessionmaker(bind=migrated_engine)
        with session_factory() as session:
            make_read_only(session)
            session.add(
                Article(
                    canonical_url="https://example.com/readonly",
                    original_url="https://example.com/readonly",
                    source_domain="example.com",
                    title="readonly",
                )
            )

            with pytest.raises(InternalError, match="read-only transaction"):
                session.flush()

    def test_rejects_delete(self, migrated_engine: Engine) -> None:
        session_factory = sessionmaker(bind=migrated_engine)
        with session_factory() as session:
            make_read_only(session)

            with pytest.raises(InternalError, match="read-only transaction"):
                session.execute(text("delete from articles"))

    def test_is_idempotent(self, migrated_engine: Engine) -> None:
        """二重に呼んでも失敗しない。呼び出し側が経路をまたいでも安全にするため。"""
        session_factory = sessionmaker(bind=migrated_engine)
        with session_factory() as session:
            make_read_only(session)
            make_read_only(session)

            assert session.execute(text("select 1")).scalar_one() == 1


class TestReadOnlySession:
    def test_opens_a_read_only_session(
        self, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """接続先はテスト用 DB へ差し替える。本番の接続設定をテストから触らないため。"""
        monkeypatch.setattr(
            "techradar.measure.session.get_session_factory",
            lambda: sessionmaker(bind=migrated_engine),
        )

        with read_only_session() as session:
            with pytest.raises(InternalError, match="read-only transaction"):
                session.execute(text("delete from articles"))

    def test_releases_the_transaction_on_exit(
        self, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """抜けたらトランザクションを解放する。計測が接続を掴んだままにしない。"""
        monkeypatch.setattr(
            "techradar.measure.session.get_session_factory",
            lambda: sessionmaker(bind=migrated_engine),
        )

        with read_only_session() as session:
            session.execute(text("select 1"))
            opened = session

        assert not opened.in_transaction()


class TestDbSessionFixtureIsUnaffected:
    def test_other_tests_can_still_write(self, db_session: Session) -> None:
        """読み取り専用化はセッション単位で、他のテストのセッションへ影響しない。"""
        db_session.add(
            Article(
                canonical_url="https://example.com/writable",
                original_url="https://example.com/writable",
                source_domain="example.com",
                title="writable",
            )
        )
        db_session.flush()

        assert db_session.query(Article).count() == 1
