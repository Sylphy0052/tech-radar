"""公式ソースレジストリのシード投入（`PROJECT_SPEC.md` §11）。

    uv run python -m techradar.sources.seed

`run.sh` がマイグレーションの後に実行する。冪等なので繰り返し実行してよい。
`verified` が立った行は手動確認済みとして上書きしない。
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from techradar.db import session_scope
from techradar.sources.config import SourceConfigError, load_registry_config
from techradar.sources.service import seed_source_registry

logger = logging.getLogger(__name__)


def _config_path(value: str) -> Path:
    """コマンドライン引数を設定ファイルのパスへ変換する。

    `argparse` が引数の誤りとして扱ってくれない入力を、ここで
    `argparse.ArgumentTypeError` へ変換する。こうすると `--force` のような
    オプション風の引数と同じ usage 表示・終了コード 2 に揃う。素通りさせると
    どれも「そんなファイルは無い」という趣旨の終了コード 1 で終わり、引数が
    間違っていたことが伝わらない（Issue #104）。

    - 空文字列と空白のみ。シェルの展開ミスで渡りうる。`Path("")` も `Path(" ")`
      も実質カレントディレクトリを指すため、ディレクトリを YAML として読もうと
      して落ちる
    - ハイフンで始まる引数。`argparse` は `-`（標準入力を表す Unix の慣習）と
      `-1` のような負数形式のトークンを、オプションではなく位置引数として受け
      取る。このコマンドは標準入力から設定を読まず、数値オプションも持たない。
      ハイフンで始まるパスを本当に渡したいときは `./-name.yaml` と書く
    - ディレクトリ。`.` や `./` を含む
    """
    if not value.strip():
        raise argparse.ArgumentTypeError("設定ファイルのパスが空です")
    if value.startswith("-"):
        raise argparse.ArgumentTypeError(
            f"不明なオプション: {value}（このコマンドにオプションはない）"
        )
    path = Path(value)
    if path.is_dir():
        raise argparse.ArgumentTypeError(f"設定ファイルではなくディレクトリを指している: {value}")
    return path


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """引数を解釈する。誤りがあれば usage を出して終了コード 2 で終わる。

    このコマンドにオプションは無いが、`--force` のような引数や 2 つ目以降の引数を
    黙って設定ファイルのパスとして扱うと、「そんなファイルは無い」という趣旨の
    エラーになるだけで、オプションが存在しないことも余分な引数を捨てたことも
    伝わらない（Issue #104）。判定は `argparse` に任せる（`measure/__main__.py` と
    同じ流儀で、自前の usage 文字列や終了コードを持たない）。`argparse` が
    見逃す入力だけを `_config_path` で拾う。

    `allow_abbrev` を切ってあるのは、既定の True では未知のオプションが前方一致で
    既知のものへ解決されるため（実測で `--he` が `--help` として通る）。誤入力を
    エラーにせず別の動作へ倒すのは、このコマンドの目的と逆になる。
    """
    parser = argparse.ArgumentParser(
        prog="python -m techradar.sources.seed",
        description="公式ソースレジストリの設定を DB へ投入する",
        allow_abbrev=False,
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=_config_path,
        # metavar は英語にする。argparse の桁揃えは文字数で計算し全角の表示幅を
        # 見ないため、日本語を置くと --help の説明列が縦に揃わない。
        metavar="PATH",
        help="読み込む設定ファイルのパス（省略時は同梱の設定）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """設定ファイルを DB へ投入する。

    Args:
        argv: 引数。1 つ目に設定ファイルのパスを指定できる（省略時は同梱設定）。

    Returns:
        終了コード。設定が壊れている場合は 1。引数が不正な場合は `argparse` が
        `SystemExit(2)` を送出するため、この関数からは返らない。
    """
    logging.basicConfig(level=logging.INFO, format="[seed] %(message)s")
    args = _parse_args(argv)

    try:
        config = load_registry_config(args.path)
    except (SourceConfigError, ValueError):
        logger.exception("ソースレジストリ設定を読み込めませんでした")
        return 1

    with session_scope() as session:
        result = seed_source_registry(session, config)

    logger.info(
        "レジストリを投入しました: 追加=%d 更新=%d 手動確認済みのためスキップ=%d",
        result.created,
        result.updated,
        result.skipped_verified,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
