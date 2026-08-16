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
#
# **テストの判定にディレクトリ名（`backend/tests/`）は使わない。** self review 指摘
# （Issue #112 の MR !139）で分かったが、このディレクトリには判定ロジックそのもの
# （`backend/tests/db_process_isolation.py` や `backend/tests/fake_worktree_roots.py`、
# `backend/tests/schema_parity.py`。いずれも CLAUDE.md が名指しする実装）や
# `conftest.py` が同居している。ディレクトリ名だけで test に倒すと、この種の実装の
# 変更が「実装0件」に埋もれて見えなくなり、実装が入っている MR まで警告してしまう。
# テストはファイル名の規約（*.test.* / *.spec.* / test_*.py / *_test.py）とテスト専用
# ディレクトリ（__tests__/、test-utils/、__mocks__/）で判定し、`backend/tests/` は
# 実装側のディレクトリ一覧に含める。`backend/tests/test_foo.py` のような通常のテスト
# ファイルは、ディレクトリより先に効くファイル名の規約で拾われるため引き続き test になる。
classify_changed_path() {
  local path="$1"

  # テスト専用ディレクトリ。ファイル名がテストの命名規約に従っていなくてもテストとして
  # 扱う（`frontend/src/test-utils/timeouts.ts` のようなヘルパーが実例）。
  case "$path" in
    */__tests__/* | __tests__/* | */test-utils/* | */__mocks__/*)
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

  # 実装。ソースツリー、実行される側のスクリプト、そして backend/tests/ 配下のうち
  # テストの命名規約に合わないファイル。**上のテスト判定が先に効くことに依存する。**
  # `frontend/*.ts` は `frontend/vitest.global-setup.test.ts` のようなテストも含む
  # パターンだが、テスト判定が先に走るため誤って impl へ落ちることはない。
  #
  # `run.sh`（CLAUDE.md が名指しする主要開発コマンド）、`backend/config/*`（収集と
  # スコアリングの挙動を実行時に左右する設定データであり、コードと同様に振る舞いを
  # 決める）を足す。self review 指摘（Issue #112 の MR !139）で、`git ls-files` の
  # 全件を分類する網羅 sweep によりこれらの欠落が判明した。
  case "$path" in
    run.sh | \
      backend/src/* | backend/scripts/* | backend/migrations/* | backend/tests/* | \
      backend/config/* | \
      frontend/src/* | frontend/eslint-rules/* | \
      scripts/* | infra/*)
      printf 'impl'
      return 0
      ;;
  esac

  # frontend/ 直下のトップレベルファイル（`next.config.ts`、`eslint.config.mjs`、
  # `postcss.config.mjs` など）。
  #
  # 以前は上の `case` に `frontend/*.ts | frontend/*.mts` を含めていたが、`case` の
  # `*` は `/` を跨ぐため `frontend/*.ts` は `frontend/deep/nested/file.ts` にも一致
  # してしまう（self review 指摘、Issue #112 の MR !139 の2巡目、実測確認済み）。
  # 現状は `frontend/src/*` と `frontend/eslint-rules/*` が先に同じ impl を返すため
  # 実害は無いが、将来 `frontend/e2e/` のようなディレクトリができたときに意図せず
  # impl へ倒れる。`case` では「スラッシュが1つだけ」を書けないため、`[[ ]]` で
  # 「frontend/ 直下に一致し、かつさらに深い階層には一致しない」ことを明示する。
  # `.mjs` も同様の理由で `case` から外し、ここへ合流させた。
  if { [[ "$path" == frontend/*.ts && "$path" != frontend/*/* ]] || \
       [[ "$path" == frontend/*.mts && "$path" != frontend/*/* ]] || \
       [[ "$path" == frontend/*.mjs && "$path" != frontend/*/* ]]; }; then
    printf 'impl'
    return 0
  fi

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
  local counts tests impls others
  counts="$(_count_diff_scope "$@")"
  IFS=' ' read -r tests impls others <<< "$counts"

  # 先例（`scripts/ai-harness/lib/machine-load.sh` の `machine_is_congested`）に
  # 合わせて `return 0` / `return 1` を明示する。`[[ ... ]]` を関数の最後の文にして
  # その終了コードへ暗黙に委ねる形は、呼び出し側から見て「この関数は真偽を返す」と
  # 読み取りにくい（self review 指摘、Issue #112 の MR !139 の2巡目）。
  if [[ "$tests" -gt 0 && "$impls" -eq 0 ]]; then
    return 0
  fi
  return 1
}

# 警告の根拠として内訳を出す。何件がテストで何件が実装かを人が読めるようにする。
describe_diff_scope() {
  local counts tests impls others
  counts="$(_count_diff_scope "$@")"
  IFS=' ' read -r tests impls others <<< "$counts"

  printf '変更 %s件: テスト %s件 / 実装 %s件 / その他 %s件' \
    "$#" "$tests" "$impls" "$others"
}
