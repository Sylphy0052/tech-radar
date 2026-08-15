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

    def test_rejects_option_like_arguments(self, cli_session: Session, capsys):
        # Arrange — このコマンドにオプションは無い。オプション風の引数を設定ファイルの
        # パスとして扱うと「そんなファイルは無い」というエラーで終わり、オプションが
        # 存在しないことが伝わらない（Issue #104）。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main(["--force"])

        # Assert — 引数の誤りとして終了コード 2 で落ち、DB は触らない
        assert excinfo.value.code == 2
        assert "--force" in capsys.readouterr().err
        assert cli_session.scalars(select(SourceRegistry)).all() == []

    def test_rejects_extra_arguments(self, cli_session: Session, tmp_path: Path, capsys):
        # Arrange — 受け付けるのは設定ファイルのパス1つだけ。2つ目を黙って捨てると、
        # 読み込んだ設定が呼び出し側の意図とずれたまま気付けない。
        first = tmp_path / "first.yaml"
        first.write_text(
            "entities:\n"
            "  - name: Example\n"
            "    rules:\n"
            "      - domain: first.example.com\n"
            "        type: official_blog\n",
            encoding="utf-8",
        )

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main([str(first), str(tmp_path / "second.yaml")])

        # Assert
        assert excinfo.value.code == 2
        assert cli_session.scalars(select(SourceRegistry)).all() == []

    def test_rejects_an_empty_path(self, cli_session: Session, capsys):
        # Arrange — 空文字列はシェルの展開ミスで渡りうる。`Path("")` はカレント
        # ディレクトリを指すため、素通りさせると「ディレクトリを YAML として読もうと
        # した」という分かりにくいエラーで落ちる。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main([""])

        # Assert
        assert excinfo.value.code == 2
        assert capsys.readouterr().err != ""
        assert cli_session.scalars(select(SourceRegistry)).all() == []
