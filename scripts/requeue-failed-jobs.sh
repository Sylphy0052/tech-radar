#!/usr/bin/env bash
# 失敗したジョブをpendingへ戻す（Issue #79）。
# 実体はbackend/scripts/requeue_failed_jobs.py。既定はdry-run、実際に戻すには --apply を渡す。
#
#   scripts/requeue-failed-jobs.sh --type embed_article                      # dry-run
#   scripts/requeue-failed-jobs.sh --type embed_article --apply              # 実行
#   scripts/requeue-failed-jobs.sh --type embed_article \
#     --error-contains "sentence-transformers" --apply                      # 理由で絞って実行
#
# リポジトリルートからでもscripts/からでも同じ結果になるよう、
# 自分の場所を起点にbackend/へcdしてから実行する。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

command -v uv >/dev/null 2>&1 || {
  echo "[requeue-failed-jobs] uv未インストール — https://astral.sh/uv" >&2
  exit 1
}

cd "$REPO_ROOT/backend"
exec uv run python -m scripts.requeue_failed_jobs "$@"
