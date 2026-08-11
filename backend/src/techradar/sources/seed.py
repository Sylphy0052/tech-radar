"""公式ソースレジストリのシード投入（`PROJECT_SPEC.md` §11）。

    uv run python -m techradar.sources.seed

`run.sh` がマイグレーションの後に実行する。冪等なので繰り返し実行してよい。
`verified` が立った行は手動確認済みとして上書きしない。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from techradar.db import session_scope
from techradar.sources.config import SourceConfigError, load_registry_config
from techradar.sources.service import seed_source_registry

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """設定ファイルを DB へ投入する。

    Args:
        argv: 引数。1 つ目に設定ファイルのパスを指定できる（省略時は同梱設定）。

    Returns:
        終了コード。設定が壊れている場合は 1。
    """
    logging.basicConfig(level=logging.INFO, format="[seed] %(message)s")
    arguments = sys.argv[1:] if argv is None else argv
    path = Path(arguments[0]) if arguments else None

    try:
        config = load_registry_config(path)
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
