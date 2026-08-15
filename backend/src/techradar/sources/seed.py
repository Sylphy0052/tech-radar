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

    空文字列はシェルの展開ミスで渡りうる。`Path("")` はカレントディレクトリを
    指すため、素通りさせるとディレクトリを YAML として読もうとして分かりにくい
    エラーになる。ここで弾けば、他の引数エラーと同じ usage 表示・終了コードで揃う。
    """
    if not value:
        raise argparse.ArgumentTypeError("設定ファイルのパスが空です")
    return Path(value)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """引数を解釈する。誤りがあれば usage を出して終了コード 2 で終わる。

    このコマンドにオプションは無いが、`-` で始まる引数や 2 つ目以降の引数を黙って
    設定ファイルのパスとして扱うと、「そんなファイルは無い」という趣旨のエラーに
    なるだけで、オプションが存在しないことも余分な引数を捨てたことも伝わらない
    （Issue #104）。判定は `argparse` に任せる（`measure/__main__.py` と同じ流儀で、
    自前の検証を持たない）。
    """
    parser = argparse.ArgumentParser(
        prog="python -m techradar.sources.seed",
        description="公式ソースレジストリの設定を DB へ投入する",
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=_config_path,
        metavar="設定ファイル",
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
