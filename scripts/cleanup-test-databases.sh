#!/usr/bin/env bash
# 削除済みworktreeに紐付くテスト用DBを掃除する（Issue #51）。
# 実体は backend/scripts/cleanup_test_databases.py。既定はdry-run、実削除には --apply を渡す。
#
#   scripts/cleanup-test-databases.sh          # dry-run
#   scripts/cleanup-test-databases.sh --apply  # 実削除
#
# リポジトリルートからでも scripts/ からでも同じ結果になるよう、
# 自分の場所を起点に backend/ へ cd してから実行する。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

command -v uv >/dev/null 2>&1 || {
  echo "[cleanup-test-databases] uv未インストール — https://astral.sh/uv" >&2
  exit 1
}

cd "$REPO_ROOT/backend"
exec uv run python -m scripts.cleanup_test_databases "$@"
