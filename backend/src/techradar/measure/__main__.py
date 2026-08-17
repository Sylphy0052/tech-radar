"""計測の実行エントリポイント（Issue #74）。

    cd backend
    uv run python -m techradar.measure          # 人が読む表形式
    uv run python -m techradar.measure --json   # 機械可読な JSON

本番 DB を読み取り専用で参照する。結果は標準出力へ出し、必要なら呼び出し側で
リダイレクトする（計測結果は Issue へコメントで残す運用のため、ファイル出力は持たない）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from techradar.config import get_settings
from techradar.measure.collect import collect_measurements
from techradar.measure.report import render_json, render_text
from techradar.measure.session import read_only_session


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m techradar.measure",
        description="パラメータ実測のための集計を出力する（Issue #73 の前提）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="機械可読な JSON で出力する",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """集計して標準出力へ書く。データが無くても 0 で終わる。"""
    args = _parse_args(argv)
    settings = get_settings()
    now = datetime.now(UTC)

    with read_only_session() as session:
        measurements = collect_measurements(session, settings=settings, now=now)

    output = render_json(measurements) if args.as_json else render_text(measurements)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
