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

    def test_rejects_an_abbreviated_option(self, cli_session: Session, capsys):
        # Arrange — argparse は既定 (allow_abbrev=True) で未知のオプションを前方一致で
        # 既知のものへ解決する。`--he` が `--help` として通ると、誤入力がエラーではなく
        # 別の動作になる。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main(["--he"])

        # Assert
        assert excinfo.value.code == 2
        assert "--he" in capsys.readouterr().err
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

        # Assert — 捨てた引数がエラーの理由として報告される
        assert excinfo.value.code == 2
        assert "second.yaml" in capsys.readouterr().err
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
        assert "パスが空です" in capsys.readouterr().err
        assert cli_session.scalars(select(SourceRegistry)).all() == []

    def test_rejects_a_whitespace_only_path(self, cli_session: Session, capsys):
        # Arrange — 空白だけの引数も空文字列と同じくカレントディレクトリ相当へ落ちる。
        # 空文字列だけを弾いても、シェルの展開ミスという同じ原因を拾いきれない。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main([" "])

        # Assert
        assert excinfo.value.code == 2
        assert "パスが空です" in capsys.readouterr().err
        assert cli_session.scalars(select(SourceRegistry)).all() == []

    def test_rejects_a_single_dash(self, cli_session: Session, capsys):
        # Arrange — argparse は `-` 単体を標準入力の慣習としてオプション扱いしない。
        # このコマンドは標準入力から設定を読まないため、`--force` と同じ扱いで弾く
        # (弾かないと「そんなファイルは無い」という終了コード 1 のエラーになり、
        # オプションが存在しないことが伝わらないという Issue #104 の問題が残る)。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main(["-"])

        # Assert
        assert excinfo.value.code == 2
        assert "-" in capsys.readouterr().err
        assert cli_session.scalars(select(SourceRegistry)).all() == []

    def test_rejects_a_negative_number_like_argument(self, cli_session: Session, capsys):
        # Arrange — 数値オプションを持たないパーサでは、`-1` のような負数形式も
        # 位置引数として通ってしまう。これも `-` 単体と同じ理由で弾く。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main(["-1"])

        # Assert
        assert excinfo.value.code == 2
        assert "-1" in capsys.readouterr().err
        assert cli_session.scalars(select(SourceRegistry)).all() == []

    def test_rejects_a_directory_path(self, cli_session: Session, tmp_path: Path, capsys):
        # Arrange — `.` や `./` のようにディレクトリを指す引数は、設定ファイルとして
        # 読もうとして分かりにくいエラーになる。引数の誤りとして先に弾く。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            seed_module.main([str(tmp_path)])

        # Assert
        assert excinfo.value.code == 2
        assert "ディレクトリ" in capsys.readouterr().err
        assert cli_session.scalars(select(SourceRegistry)).all() == []
