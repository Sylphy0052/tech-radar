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
  # `|| [[ -n "$line" ]]` を付けて、末尾に改行の無い最後の行も拾う。`read` は
  # 改行が来ないと非ゼロで終わるため、これが無いと最終行が黙って消える。
  while IFS= read -r line || [[ -n "$line" ]]; do
    # 末尾の CR を落とす。CRLF 改行のテキスト（Windows 側で作った差分や
    # core.autocrlf の設定によっては `git diff --name-only` の出力にも起こりうる）を
    # そのまま流すと、`backend/tests/test_only_red.py\r` のように CR が付いたまま
    # `classify_changed_path` へ渡り、`test_*.py` の命名規約に一致しなくなって
    # `backend/tests/*` の実装パターンへ落ちる。テストの変更が実装として数えられ、
    # 警告すべき MR が rc=0（警告なし）で通り抜けていた（self review 指摘、Issue #112
    # の MR !139 の2巡目、実測確認済み）。
    #
    # glab 経路（下の else 節）では同じ処理をしない。JSON から取り出したパスに CR が
    # 含まれているなら、それは端末由来の改行ノイズではなく本物のファイル名の一部で
    # ある可能性が高く、勝手に削ると別のファイルとして扱ってしまう。
    line="${line%$'\r'}"
    [[ -n "$line" ]] && CHANGED_PATHS+=("$line")
  done
else
  MR_IID="$1"
  [[ "$MR_IID" =~ ^[0-9]+$ ]] || usage
  command -v glab >/dev/null 2>&1 || fail "glab が見つかりません（このリポジトリは gh ではなく glab を使います）"
  command -v jq >/dev/null 2>&1 || fail "jq が見つかりません"

  # `2>/dev/null` は付けない。glab 自身のエラー（認証切れ・404・レート制限）が
  # 見えなくなり、下の fail の汎用メッセージだけでは原因を診断できなくなるため。
  RAW="$(glab api "projects/:id/merge_requests/${MR_IID}/changes")" \
    || fail "MR !${MR_IID} の変更を取得できませんでした（glab api が非0で終了しました）"

  # `.changes` が配列であることをプロセス置換の外で明示的に確認する。ここを飛ばすと、
  # `{"message":"..."}` のような認証エラー応答や非JSONが来たときに jq が失敗しても
  # プロセス置換の中で握り潰され、CHANGED_PATHS が空のまま rc=0（警告なし）で
  # 正常終了してしまう（self review 指摘、Issue #112 の MR !139）。
  printf '%s' "$RAW" | jq -e '.changes | type == "array"' >/dev/null \
    || fail "MR !${MR_IID} の応答に .changes 配列がありません（glab のエラー応答や認証切れの可能性があります）: $(printf '%s' "$RAW" | head -c 200)"

  # GitLab の changes API は大きな MR で差分を切り詰めることがある（`overflow: true`）。
  # 切り詰められた場合、末尾側のファイルが `.changes` から欠落する。テストファイルだけが
  # 切り詰められると tests=0 になり、この検査そのものが無警告方向でスキップされる。
  # 「一部だけ見て判定できた」ことにせず、判定できないものとして rc=2 で止める
  # （両方のレビュー視点が指摘、Issue #112 の MR !139 の2巡目）。
  printf '%s' "$RAW" | jq -e '.overflow != true' >/dev/null \
    || fail "MR !${MR_IID} の差分が大きすぎて GitLab 側で切り詰められています（overflow）。changes API では全件を判定できません。"

  # パスの抽出は一時ファイルを経由する。
  #
  # 以前はここをプロセス置換（`< <(...)`）にしていたが、プロセス置換の中身は
  # サブシェルで実行されるため、その中の `jq` が失敗しても終了コードが親シェルへ
  # 伝わらない。`.changes` の要素が想定外の形（文字列と混在している等）だと
  # `.new_path` の参照で `jq` がエラー終了するが、以前の実装はこれを無視して
  # CHANGED_PATHS が欠けたまま rc=0（またはズレた rc=1）で正常終了していた
  # （両方のレビュー視点が指摘、Issue #112 の MR !139 の2巡目、実測確認済み）。
  # 一時ファイルへ書き出せば、下の `|| fail` で抽出そのものの失敗を rc=2 で止められる。
  #
  # `.new_path // .old_path // empty` にする。`new_path` を持たない要素（一部の
  # 削除応答等）があっても件数から黙って消えないようにする。
  #
  # NUL 区切りで読む。改行を含むファイル名を挟むと、改行区切りの読み取りでは2件に
  # 分裂して内訳の件数がずれる。コマンド置換（`$(...)`）は NUL を保持できないため
  # 一時ファイルにした。
  TMP_PATHS="$(mktemp)"
  trap 'rm -f "$TMP_PATHS"' EXIT
  printf '%s' "$RAW" \
    | jq -j '.changes[] | ((.new_path // .old_path // empty) + "\u0000")' > "$TMP_PATHS" \
    || fail "MR !${MR_IID} の .changes 内に想定外の形の要素があります（パスの抽出に失敗しました）"

  while IFS= read -r -d '' path; do
    CHANGED_PATHS+=("$path")
  done < "$TMP_PATHS"
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
