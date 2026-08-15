"""OpenAPI スキーマの書き出し（`PROJECT_SPEC.md` §24 型安全性）。

    uv run python -m techradar.openapi_export

frontend の `npm run gen:api-types` がこの出力（既定では `backend/openapi.json`）
から TypeScript 型を生成し、DB / API / UI の型不整合を防ぐ。

スキーマは FastAPI のルーティング定義（Pydantic モデル）だけから決まり、
ハンドラを実行しないため DB 接続は不要。ただし `create_app` へは
テストと同じ `Settings(_env_file=None)` を注入し、ローカルの `.env` の
有無や中身に左右されない決定的な出力にする。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from techradar.config import REPO_ROOT, Settings
from techradar.main import create_app

DEFAULT_OUTPUT_PATH = REPO_ROOT / "backend" / "openapi.json"


def _output_path(value: str) -> Path:
    """コマンドライン引数を書き出し先のパスへ変換する。

    `argparse` が引数の誤りとして扱ってくれない入力を、ここで
    `argparse.ArgumentTypeError` へ変換する。こうすると `--check` のような
    オプション風の引数と同じ usage 表示・終了コード 2 に揃う。素通りさせると
    どれも意図しない場所へファイルを作るか、生のトレースバックで落ちる。

    - 空文字列と空白のみ。シェルの展開ミスで渡りうる。`Path("")` も `Path(" ")`
      も実質カレントディレクトリを指すため、書き出し時に `IsADirectoryError`
      になる
    - ハイフンで始まる引数。`argparse` は `-`（標準入出力を表す Unix の慣習）と
      `-1` のような負数形式のトークンを、オプションではなく位置引数として受け
      取る。このモジュールは標準出力へ書き出さず、数値オプションも持たない。
      素通りさせると `-` や `-1` という名前のファイルが作られ、`git add -A` で
      commit へ紛れ込む（Issue #103 で `--check` について実際に起きた）。
      ハイフンで始まるパスを本当に渡したいときは `./-name.json` と書く
    - ディレクトリ。`.` や `./` を含む

    親ディレクトリが存在しない場合は弾かない。それは引数として壊れているのでは
    なく書き込み時の問題であり、`write_text` の `FileNotFoundError` に任せる。
    """
    if not value.strip():
        raise argparse.ArgumentTypeError("出力先パスが空です")
    if value.startswith("-"):
        raise argparse.ArgumentTypeError(
            f"不明なオプション: {value}（このコマンドにオプションはない）"
        )
    path = Path(value)
    if path.is_dir():
        raise argparse.ArgumentTypeError(f"出力先がディレクトリを指している: {value}")
    return path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """引数を解釈する。誤りがあれば usage を出して終了コード 2 で終わる。

    このコマンドにオプションは無いが、`--check` のような引数をそのまま出力先として
    扱うと `backend/--check` というファイルが作られ、`git add -A` で commit へ紛れ
    込む（Issue #103 で実際に起きた）。判定は `argparse` に任せる（`sources/seed.py`
    と同じ流儀で、自前の usage 文字列や終了コードを持たない）。`argparse` が見逃す
    入力だけを `_output_path` で拾う。

    `allow_abbrev` を切ってあるのは、既定の True では未知のオプションが前方一致で
    既知のものへ解決されるため。誤入力をエラーにせず別の動作へ倒すのは、引数を
    検証する目的と逆になる。
    """
    parser = argparse.ArgumentParser(
        prog="python -m techradar.openapi_export",
        description="OpenAPI スキーマを JSON ファイルへ書き出す",
        allow_abbrev=False,
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=_output_path,
        # metavar は英語にする。argparse の桁揃えは文字数で計算し全角の表示幅を
        # 見ないため、日本語を置くと --help の説明列が縦に揃わない。
        metavar="PATH",
        help="書き出し先のパス（省略時は backend/openapi.json）",
    )
    return parser.parse_args(argv)


def build_openapi_schema() -> dict[str, Any]:
    """DB 接続なしで FastAPI アプリを組み立て、OpenAPI スキーマを返す。"""
    app = create_app(Settings(_env_file=None))
    return app.openapi()


def render_openapi_schema(schema: dict[str, Any]) -> str:
    """スキーマを決定的な JSON 文字列へ整形する。

    キー順序とインデントを固定し、実行のたびに差分が出ないようにする。
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """OpenAPI スキーマをファイルへ書き出す。

    Args:
        argv: 引数。1つ目に出力先パスを指定できる（省略時は `backend/openapi.json`）。

    Returns:
        終了コード。成功なら 0。引数が不正な場合は `argparse` が `SystemExit(2)` を
        送出するため、この関数からは返らない（このときファイルは作らない）。
    """
    args = _parse_args(argv)
    output_path = DEFAULT_OUTPUT_PATH if args.path is None else args.path

    schema = build_openapi_schema()
    output_path.write_text(render_openapi_schema(schema), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
