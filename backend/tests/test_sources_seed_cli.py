"""シード投入コマンドを検証する。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.db import SourceRegistry
from techradar.sources import seed as seed_module


@pytest.fixture
def cli_session(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    """`session_scope` をテスト用セッションへ差し替える。"""

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(seed_module, "session_scope", fake_scope)
    return db_session


class TestSeedCli:
    def test_seeds_the_bundled_registry(self, cli_session: Session):
        # Arrange / Act
        exit_code = seed_module.main([])

        # Assert
        assert exit_code == 0
        assert cli_session.scalars(select(SourceRegistry)).all()

    def test_accepts_an_explicit_configuration_path(self, cli_session: Session, tmp_path: Path):
        # Arrange
        path = tmp_path / "sources.yaml"
        path.write_text(
            "entities:\n"
            "  - name: Example\n"
            "    rules:\n"
            "      - domain: cli.example.com\n"
            "        type: official_blog\n",
            encoding="utf-8",
        )

        # Act
        exit_code = seed_module.main([str(path)])

        # Assert
        assert exit_code == 0
        rows = cli_session.scalars(
            select(SourceRegistry).where(SourceRegistry.domain == "cli.example.com")
        ).all()
        assert len(rows) == 1

    def test_reports_a_broken_configuration(self, cli_session: Session, tmp_path: Path):
        # Arrange — 壊れた設定で DB を触らせない
        path = tmp_path / "sources.yaml"
        path.write_text("entities: [\n", encoding="utf-8")

        # Act
        exit_code = seed_module.main([str(path)])

        # Assert
        assert exit_code == 1
        assert cli_session.scalars(select(SourceRegistry)).all() == []
