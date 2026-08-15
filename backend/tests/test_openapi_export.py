"""OpenAPI スキーマ書き出し（`PROJECT_SPEC.md` §24 型安全性）を検証する。

DB や外部サービスを必要としないことがこの機能の要件のため、
どのテストも `db_session` フィクスチャ（PostgreSQL 接続）を使わない。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from techradar.openapi_export import build_openapi_schema, main, render_openapi_schema


def test_build_openapi_schema_contains_known_paths():
    # Arrange / Act
    schema = build_openapi_schema()

    # Assert — 実装済みエンドポイントがスキーマに含まれること
    assert "/api/health" in schema["paths"]
    assert "/api/articles" in schema["paths"]
    assert "/api/crawl/runs" in schema["paths"]
    assert "/api/jobs/{job_id}" in schema["paths"]


def test_build_openapi_schema_is_deterministic():
    # Arrange / Act — 2 回生成しても同一内容であること
    first = build_openapi_schema()
    second = build_openapi_schema()

    # Assert
    assert first == second
    assert render_openapi_schema(first) == render_openapi_schema(second)


def test_render_openapi_schema_sorts_keys_and_ends_with_newline():
    # Arrange
    schema = {"b": 1, "a": 2}

    # Act
    rendered = render_openapi_schema(schema)

    # Assert — キー順序を固定し、差分が出ないよう改行で終端する
    assert rendered == json.dumps({"a": 2, "b": 1}, indent=2, sort_keys=True) + "\n"


def test_main_writes_schema_to_the_given_path(tmp_path: Path):
    # Arrange
    output_path = tmp_path / "openapi.json"

    # Act
    exit_code = main([str(output_path)])

    # Assert
    assert exit_code == 0
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert "/api/health" in written["paths"]


def test_main_defaults_to_the_repository_openapi_json_path(monkeypatch, tmp_path: Path):
    # Arrange — 既定の出力先を差し替えて、リポジトリ内の実ファイルを汚さない
    from techradar import openapi_export as module

    default_path = tmp_path / "openapi.json"
    monkeypatch.setattr(module, "DEFAULT_OUTPUT_PATH", default_path)

    # Act
    exit_code = main([])

    # Assert
    assert exit_code == 0
    assert default_path.exists()


@pytest.fixture
def isolated_output(monkeypatch, tmp_path: Path) -> Path:
    """既定の出力先とカレントディレクトリを `tmp_path` へ閉じ込める。

    引数検証のテストは「どこにもファイルを作らない」ことを見る。既定の出力先を
    差し替えないとリポジトリ内の `backend/openapi.json` が書き換わり、相対パスを
    渡すテストではカレントディレクトリにファイルが残る。
    """
    from techradar import openapi_export as module

    monkeypatch.setattr(module, "DEFAULT_OUTPUT_PATH", tmp_path / "openapi.json")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestArgumentValidation:
    """引数の誤りは終了コード 2 で落ち、ファイルを作らない。

    判定は `argparse` に任せる（Issue #106）。`argparse` は誤りを見つけると usage を
    stderr へ出して `SystemExit(2)` を送出するため、`main` からは返らない。
    """

    def test_rejects_option_like_arguments(self, isolated_output: Path, capsys):
        # Arrange — このモジュールにオプションは無い。`--check` のような引数を出力パスと
        # して扱うと `backend/--check` のようなファイルが作られ、`git add -A` で commit へ
        # 紛れ込む (Issue #103 で実際に起きた)。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["--check"])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "--check" in capsys.readouterr().err

    def test_rejects_an_abbreviated_option(self, isolated_output: Path, capsys):
        # Arrange — argparse は既定 (allow_abbrev=True) で未知のオプションを前方一致で
        # 既知のものへ解決する。`--che` が `--check`… ではなく `--help` のような既知の
        # オプションへ化けると、誤入力がエラーではなく別の動作になる。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["--che"])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "--che" in capsys.readouterr().err

    def test_rejects_extra_arguments(self, isolated_output: Path, capsys):
        # Arrange — 受け付けるのは出力パス1つだけ。2つ目を黙って捨てると、書き出し先が
        # 呼び出し側の意図とずれたまま気付けない。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main([str(isolated_output / "first.json"), str(isolated_output / "second.json")])

        # Assert — 捨てた引数がエラーの理由として報告される
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "second.json" in capsys.readouterr().err

    def test_reports_the_unknown_option_even_with_extra_arguments(
        self, isolated_output: Path, capsys
    ):
        # Arrange — `--check foo.json` は「オプション風」と「引数が多い」の両方に当たる。
        # 誤用の実態はオプションの取り違えなので、そちらが報告されてほしい。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["--check", str(isolated_output / "openapi.json")])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "--check" in capsys.readouterr().err

    def test_rejects_an_empty_path(self, isolated_output: Path, capsys):
        # Arrange — 空文字列はシェルの展開ミスで渡りうる。`Path("")` はカレント
        # ディレクトリを指すため、素通りさせると書き出し時に IsADirectoryError の
        # 生トレースバックで落ちる。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main([""])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "パスが空です" in capsys.readouterr().err

    def test_rejects_a_whitespace_only_path(self, isolated_output: Path, capsys):
        # Arrange — 空白だけの引数も空文字列と同じ原因 (シェルの展開ミス) で渡りうる。
        # 素通りさせると、空白 1 文字を名前に持つファイルが黙って作られる
        # (空文字列と違い IsADirectoryError にはならないため、気付く機会が無い)。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main([" "])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "パスが空です" in capsys.readouterr().err

    def test_rejects_a_single_dash(self, isolated_output: Path, capsys):
        # Arrange — argparse は `-` 単体を標準出力の慣習としてオプション扱いせず、
        # 位置引数として受け取る。このモジュールは標準出力へ書き出さないため、
        # 素通りさせると `-` という名前のファイルが作られてしまう。
        #
        # 理由まで見るのは、usage 行に `[-h]` が含まれており `"-" in err` では
        # どんな失敗でも通ってしまうため。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["-"])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "不明なオプション: -" in capsys.readouterr().err

    def test_rejects_a_negative_number_like_argument(self, isolated_output: Path, capsys):
        # Arrange — 数値オプションを持たないパーサでは、`-1` のような負数形式も
        # 位置引数として通ってしまう。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["-1"])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "不明なオプション: -1" in capsys.readouterr().err

    def test_rejects_a_directory_path(self, isolated_output: Path, capsys):
        # Arrange — `.` のようにディレクトリを指す引数は、素通りさせると書き出し時に
        # 未処理の IsADirectoryError で落ちる (終了コードが 2 に揃わず、生トレース
        # バックが出る)。argparse へ寄せる前の実装が実際にそうなっていた。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["."])

        # Assert
        assert excinfo.value.code == 2
        assert list(isolated_output.iterdir()) == []
        assert "ディレクトリ" in capsys.readouterr().err

    def test_shows_help_without_writing_a_file(self, isolated_output: Path, capsys):
        # Arrange — argparse へ寄せたことで `-h` が新たに使えるようになった。
        # 引数エラーと違って終了コードは 0 だが、ファイルを作らないことは同じ。

        # Act
        with pytest.raises(SystemExit) as excinfo:
            main(["-h"])

        # Assert
        assert excinfo.value.code == 0
        assert list(isolated_output.iterdir()) == []
        assert "usage: python -m techradar.openapi_export" in capsys.readouterr().out
