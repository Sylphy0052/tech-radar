#!/usr/bin/env bash
# MR の diff に実装が伴っているかを、変更ファイルの一覧だけから読む（Issue #112）。
#
# Issue #109 では、RED のテスト404行だけを含む MR が `Closes #109` 付きで main へ
# マージされ、Issue が close された。GREEN の実装は書かれていたが、MR を作った
# ブランチの派生元が RED commit の時点のままだったため diff に入らなかった。
# main は pytest の収集段階で落ちる状態になり、誰も気付かないまま残った（Issue #111）。
#
# このリポジトリには、これを検出する仕組みが無い。commit 前の `check.sh` 自動実行は
# 廃止済み（Issue #76）、MR の CI も停止済み（Issue #82）、`gitlab-mr-review` の
# self review は diff を読むが「diff に**無い**もの」は見ない。RED のテストだけの
# diff は、テスト単体としては整合しているため指摘に上がらない。
#
# 判定は変更ファイルの一覧だけを入力にする。`glab` や `git` の呼び出しは
# `check-mr-scope.sh` 側に置く。外部コマンドをここへ入れると、テストが実際の MR や
# ネットワークに依存してしまう。
#
# 呼び出し元の `log` / `fail` を使う。単体で実行するものではない。

# パスを "test" / "impl" / "other" のいずれかへ分ける。
#
# **テストか否かを先に見る。** 実装のディレクトリ配下にテストが置かれることがあり
# （`frontend/src/lib/api.test.ts` のように実装と同居する形が本リポジトリの流儀）、
# 実装として数えると「実装が入っている」と誤認してこの検査が素通りする。
classify_changed_path() {
  local path="$1"

  # テスト。ファイル名の規約（*.test.* / *.spec.* / test_*.py / *_test.py）と、
  # テスト専用のディレクトリ（backend/tests/ と __tests__/）の両方を見る。
  case "$path" in
    backend/tests/* | */__tests__/* | __tests__/*)
      printf 'test'
      return 0
      ;;
  esac
  local base="${path##*/}"
  case "$base" in
    *.test.* | *.spec.* | test_*.py | *_test.py)
      printf 'test'
      return 0
      ;;
  esac

  # 実装。ソースツリーと、実行される側のスクリプト。
  case "$path" in
    backend/src/* | backend/migrations/* | frontend/src/* | frontend/eslint-rules/* | scripts/*)
      printf 'impl'
      return 0
      ;;
  esac

  # それ以外（docs/、リポジトリ直下の Markdown、各種の設定ファイル）。
  printf 'other'
}

# 変更ファイルの一覧を受け取り、カテゴリごとの件数を "test impl other" の順で返す。
_count_diff_scope() {
  local tests=0 impls=0 others=0 path kind
  for path in "$@"; do
    kind="$(classify_changed_path "$path")"
    case "$kind" in
      test) tests=$((tests + 1)) ;;
      impl) impls=$((impls + 1)) ;;
      *) others=$((others + 1)) ;;
    esac
  done
  printf '%s %s %s' "$tests" "$impls" "$others"
}

# 「テストの変更があり、実装の変更が1つも無い」なら 0（警告すべき）を返す。
#
# 条件を「テストのみ」ではなく「実装が無い」にしてある。Issue #109 の MR は RED の
# テストだけだったが、テストと ADR だけ・テストとドキュメントだけ、という形でも同じ
# 事故になる。実装の有無で見れば、この種を一様に捕まえられる。
#
# テストの変更を含まない MR（ドキュメントのみ、実装のみ）は対象外。「実装にテストが
# 無い」ことも問題ではあるが、それはこの検査が扱う失敗とは別のものなので混ぜない。
diff_lacks_implementation() {
  local counts tests impls
  counts="$(_count_diff_scope "$@")"
  tests="${counts%% *}"
  impls="$(printf '%s' "$counts" | cut -d' ' -f2)"

  [[ "$tests" -gt 0 && "$impls" -eq 0 ]]
}

# 警告の根拠として内訳を出す。何件がテストで何件が実装かを人が読めるようにする。
describe_diff_scope() {
  local counts tests impls others
  counts="$(_count_diff_scope "$@")"
  tests="${counts%% *}"
  impls="$(printf '%s' "$counts" | cut -d' ' -f2)"
  others="${counts##* }"

  printf '変更 %s件: テスト %s件 / 実装 %s件 / その他 %s件' \
    "$#" "$tests" "$impls" "$others"
}
