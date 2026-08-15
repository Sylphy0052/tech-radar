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

import json
import sys
from pathlib import Path
from typing import Any

from techradar.config import REPO_ROOT, Settings
from techradar.main import create_app

DEFAULT_OUTPUT_PATH = REPO_ROOT / "backend" / "openapi.json"

_USAGE = "使い方: python -m techradar.openapi_export [出力先パス]"

# 引数の検証が失敗したときの終了コード。慣例（`argparse` が使う値）に合わせる。
_USAGE_ERROR_EXIT_CODE = 2


def build_openapi_schema() -> dict[str, Any]:
    """DB 接続なしで FastAPI アプリを組み立て、OpenAPI スキーマを返す。"""
    app = create_app(Settings(_env_file=None))
    return app.openapi()


def render_openapi_schema(schema: dict[str, Any]) -> str:
    """スキーマを決定的な JSON 文字列へ整形する。

    キー順序とインデントを固定し、実行のたびに差分が出ないようにする。
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    """OpenAPI スキーマをファイルへ書き出す。

    受け付ける引数は出力先パス1つだけで、オプションは持たない。`-` で始まる引数を
    そのまま出力先として扱うと、`--check` のような誤った呼び出しで `backend/--check`
    というファイルが作られ、`git add -A` で commit へ紛れ込む（Issue #103 で実際に
    起きた）。オプションらしき引数と余分な引数は、書き出す前に弾く。

    Args:
        argv: 引数。1つ目に出力先パスを指定できる（省略時は `backend/openapi.json`）。

    Returns:
        終了コード。成功なら 0、引数が不正なら 2（このときファイルは作らない）。
    """
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print(f"引数が多すぎます: {' '.join(arguments)}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return _USAGE_ERROR_EXIT_CODE
    if arguments and arguments[0].startswith("-"):
        print(f"不明なオプション: {arguments[0]}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return _USAGE_ERROR_EXIT_CODE
    output_path = Path(arguments[0]) if arguments else DEFAULT_OUTPUT_PATH

    schema = build_openapi_schema()
    output_path.write_text(render_openapi_schema(schema), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
