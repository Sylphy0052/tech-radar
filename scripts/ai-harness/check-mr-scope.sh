#!/usr/bin/env bash
# MR の diff に実装が伴っているかを見る（Issue #112）。
#
# 使い方:
#   scripts/ai-harness/check-mr-scope.sh <MR_IID>   # glab で変更ファイルを取って判定する
#   scripts/ai-harness/check-mr-scope.sh --stdin    # 変更ファイルを1行1件で標準入力から読む
#
# 終了コード:
#   0  実装が伴っている、またはテストの変更を含まない（この検査の対象外）
#   1  テストの変更があり、実装の変更が1つも無い（マージ前に受入基準と突き合わせること）
#   2  引数や外部コマンドの問題で判定できなかった
#
# **1 は拒否ではなく警告である。** テストの追補だけを行う正当な MR でも 1 になる。
# 意図した追補であればそのまま進めてよい。止めたいのは「実装が diff に入らないまま
# Issue が close される」ことであり、そのために人が一度目を通す機会を作るのが目的。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[check-mr-scope] %s\n' "$*" >&2; }
fail() { printf '[check-mr-scope] %s\n' "$*" >&2; exit 2; }

# shellcheck source=lib/mr-diff-scope.sh
source "$SCRIPT_DIR/lib/mr-diff-scope.sh"

usage() {
  printf 'usage: %s <MR_IID> | --stdin\n' "${0##*/}" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage

CHANGED_PATHS=()

if [[ "$1" == "--stdin" ]]; then
  # 空行は捨てる。`git diff --name-only` の出力をそのまま流せるようにする。
  while IFS= read -r line; do
    [[ -n "$line" ]] && CHANGED_PATHS+=("$line")
  done
else
  MR_IID="$1"
  [[ "$MR_IID" =~ ^[0-9]+$ ]] || usage
  command -v glab >/dev/null 2>&1 || fail "glab が見つかりません（このリポジトリは gh ではなく glab を使います）"
  command -v jq >/dev/null 2>&1 || fail "jq が見つかりません"

  # `new_path` を見る。リネームや削除でも新しい側のパスで分類してよい（削除された
  # テストは「テストの変更」であり、実装が伴わないなら警告の対象に入る）。
  RAW="$(glab api "projects/:id/merge_requests/${MR_IID}/changes" 2>/dev/null)" \
    || fail "MR !${MR_IID} の変更を取得できませんでした"
  while IFS= read -r line; do
    [[ -n "$line" ]] && CHANGED_PATHS+=("$line")
  done < <(printf '%s' "$RAW" | jq -r '.changes[]?.new_path // empty')
fi

log "$(describe_diff_scope "${CHANGED_PATHS[@]+"${CHANGED_PATHS[@]}"}")"

if diff_lacks_implementation "${CHANGED_PATHS[@]+"${CHANGED_PATHS[@]}"}"; then
  log "テストの変更があるのに、実装の変更が1つも入っていません。"
  log "この MR が閉じる Issue の受入基準を満たす変更が diff に入っているか、マージ前に確認してください。"
  log "TDD で RED と GREEN を別 commit にした場合は、両方が同じ MR に入っているかを見ます"
  log "（Issue #109 では RED だけがマージされ、実装は #111 まで main へ届きませんでした）。"
  log "テストの追補だけを意図した MR であれば、このまま進めて構いません。"
  exit 1
fi

log "この検査では警告しません（実装の変更が入っている、またはテストの変更を含まないため対象外です）。"
