#!/usr/bin/env bash
# backend / frontend の lint・format・型チェック・テスト・依存監査・secret検知を
# 一括実行する。
#
# 手動で実行する。commit 前の自動実行は 2026-08-12 に廃止し（Issue #76）、CI も
# 止めた（Issue #82）ため、これを回さない限り壊れたことに気付く機会が無い。
#
# 互いに独立したチェックは並列で走らせる（Issue #61）。直列だと backend の
# pytest が終わるまで frontend が始まらず、待ち時間がそのまま足し算になる。
# 検証する内容は直列版と同じで、実行順序だけを変えている。
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
log() { printf '[check] %s\n' "$*" >&2; }
fail() { printf '[check][FAIL] %s\n' "$*" >&2; exit 1; }

COMPOSE_FILE="infra/docker-compose.yml"
# docker へ到達できないときの案内へ埋め込む。run.sh と共有するライブラリ側からは
# 呼び出し元のパスが分からないため、ここで渡す。
ENTRYPOINT_SCRIPT="${BASH_SOURCE[0]}"

# PostgreSQL の起動確認は run.sh と共有する（Issue #55）。
# shellcheck source=lib/postgres.sh
source "$REPO_ROOT/scripts/ai-harness/lib/postgres.sh"

# pytest と vitest をそれぞれ何プロセスへ分散させるか。
#
# pytest はワーカーごとにテスト用 DB を作り直すため（Issue #33）、CPU 数ぶんまで
# 増やすと PostgreSQL 側が重くなる。vitest は既定で CPU 数ぶん起動するため、
# 制限しないと pytest と奪い合って両方とも単独実行より遅くなる。
#
# 22 コア機での実測では 8 + 8 が最も速かった（Issue #61）。コア数の少ない環境で
# 同じ値を使うと過剰になるため、コア数の半分と 8 の小さい方を既定にする
# （22 コアなら 8 で実測どおり、8 コアなら 4、2 コアなら 1）。環境変数を明示すれば
# その値をそのまま使う。`PYTEST_WORKERS=1` は xdist を使わない単一プロセス実行。
_default_workers() {
  local cores half
  cores="$(nproc 2>/dev/null || echo 1)"
  half=$((cores / 2))
  ((half < 1)) && half=1
  ((half > 8)) && half=8
  printf '%s' "$half"
}
PYTEST_WORKERS="${PYTEST_WORKERS:-$(_default_workers)}"
VITEST_WORKERS="${VITEST_WORKERS:-$(_default_workers)}"

# 依存脆弱性監査の上限時間。実測は uv audit が約2秒、npm audit が約1秒なので、
# ここへ到達するのは相手側 (OSV / npm registry) が応答を返さないときだけである。
# 上限が無いと wait_jobs が待ち続け、check.sh 全体が終わらなくなる（Issue #83）。
AUDIT_TIMEOUT_SECONDS="${AUDIT_TIMEOUT_SECONDS:-120}"

# 並列実行するジョブを、ラベル・PID・出力先の3つの並びで管理する。
JOB_LABELS=()
JOB_PIDS=()
JOB_LOGS=()

# ジョブごとに独立したプロセスグループを作る。中断されたときに、ジョブが起動した
# 子孫（pytest のワーカー、vitest の fork）までまとめて止めるために要る。
set -m

# 中断時に、走っているジョブと一時ファイルを片付ける。
# このスクリプト自身だけを終了させた場合（hook や CI のタイムアウトによる kill、
# Ctrl-C）、後始末をしないと pytest や vitest のワーカーが生き残る。残った
# ワーカーはテスト用 DB への接続を掴んだままになり、接続が残っている DB は
# 孤児掃除の対象外にしてあるため（Issue #33）、DB が回収されなくなる。
cleanup_jobs() {
  local pid
  for pid in ${JOB_PIDS[@]+"${JOB_PIDS[@]}"}; do
    # プロセスグループごと止める。ジョブのサブシェルだけを止めても、その先の
    # pytest や vitest は親を失って動き続ける（実測で確認）。
    kill -- "-$pid" 2>/dev/null || true
  done
  ((${#JOB_LOGS[@]} > 0)) && rm -f "${JOB_LOGS[@]}"
  return 0
}
trap cleanup_jobs EXIT
trap 'cleanup_jobs; exit 130' INT
trap 'cleanup_jobs; exit 143' TERM

# ジョブをバックグラウンドで開始する。出力は端末へ流さずファイルへ溜めるため、
# 何が走っているかはここで先に出す。溜めた出力は最後にまとめて出るので、
# これが無いと数十秒のあいだ端末が無言になり、停止と区別が付かない。
#
# 標準入力は `/dev/null` へ向ける。複数のジョブが端末の標準入力を共有すると、
# どれかが入力待ちに入ったときに取り合いになる。
start_job() {
  local label="$1"
  shift
  local logfile
  logfile="$(mktemp)"
  log "開始: $label"
  ("$@") </dev/null >"$logfile" 2>&1 &
  JOB_LABELS+=("$label")
  JOB_PIDS+=("$!")
  JOB_LOGS+=("$logfile")
}

# 全ジョブの完了を待ち、開始順に出力をまとめて流す。走らせたまま流すと別ジョブの
# 行が割り込んで失敗箇所が読めなくなるため、ここまで溜めてから出す。
# 失敗したジョブがあっても全ジョブの出力を出し切ってから落ちる。1 回の実行で
# 落ちている箇所を全部見せるため。
wait_jobs() {
  local failed=() index rc
  for index in "${!JOB_PIDS[@]}"; do
    rc=0
    wait "${JOB_PIDS[$index]}" || rc=$?
    printf -- '---- %s ----\n' "${JOB_LABELS[$index]}" >&2
    cat "${JOB_LOGS[$index]}" >&2
    rm -f "${JOB_LOGS[$index]}"
    ((rc == 0)) || failed+=("${JOB_LABELS[$index]}")
  done
  JOB_LABELS=()
  JOB_PIDS=()
  JOB_LOGS=()
  ((${#failed[@]} == 0)) || fail "失敗したチェック: ${failed[*]}"
}

# ---- backend (Python / uv) のジョブ ----
backend_lint() {
  cd "$REPO_ROOT/backend"
  log "backend: ruff check"
  uv run ruff check . || fail "backend: ruff check失敗"
  log "backend: ruff format --check"
  uv run ruff format --check . || fail "backend: ruff format --check失敗"
  log "backend: ty check"
  uv run ty check || fail "backend: ty check失敗"
}

backend_pytest() {
  cd "$REPO_ROOT/backend"
  log "backend: pytest (ワーカー数: $PYTEST_WORKERS)"
  if [[ "$PYTEST_WORKERS" == "1" ]]; then
    uv run pytest || fail "backend: pytest失敗"
  else
    uv run pytest -n "$PYTEST_WORKERS" || fail "backend: pytest失敗"
  fi
}

backend_audit() {
  cd "$REPO_ROOT/backend"
  log "backend: uv audit"
  # `uv audit` は uv 0.12 時点で experimental のため、明示しないと毎回警告が出る。
  # 参照先は OSV なのでネットワークが要る（Issue #83）。応答が返らないまま
  # 待ち続けると wait_jobs ごと止まるため、timeout で頭を押さえる。
  timeout "$AUDIT_TIMEOUT_SECONDS" uv audit --preview-features audit-command \
    || fail "backend: uv audit失敗（依存に既知脆弱性があるか、OSVへ到達できません）"
}

backend_openapi_freshness() {
  cd "$REPO_ROOT/backend"
  log "backend: openapi.jsonの鮮度チェック"
  # 鮮度チェックに引っかかった場合も消す。`fail` はこのジョブのサブシェルごと
  # 終了させるため、末尾に `rm` を置くだけでは失敗経路で残る。
  #
  # `local` にはしない。EXIT trap が発火する時点では関数のスコープを抜けており、
  # `set -u` の下では unbound variable で落ちる（実測で確認）。ジョブはサブシェル
  # として起動するため、ここでグローバルへ置いても他のジョブには影響しない。
  freshness_tmp="$(mktemp --suffix=.json)"
  trap 'rm -f "$freshness_tmp"' EXIT
  uv run python -m techradar.openapi_export "$freshness_tmp" \
    || fail "backend: openapi.jsonの生成に失敗しました"
  diff -q openapi.json "$freshness_tmp" >/dev/null \
    || fail "backend: openapi.jsonが最新ではありません（uv run python -m techradar.openapi_exportで再生成してcommitしてください）"
}

# ---- frontend (TypeScript / npm) のジョブ ----
frontend_eslint() {
  cd "$REPO_ROOT/frontend"
  log "frontend: eslint"
  npm run lint || fail "frontend: lint失敗"
}

frontend_typecheck() {
  cd "$REPO_ROOT/frontend"
  log "frontend: tsc --noEmit"
  npm run typecheck || fail "frontend: typecheck失敗"
}

frontend_vitest() {
  cd "$REPO_ROOT/frontend"
  log "frontend: vitest (ワーカー数: $VITEST_WORKERS)"
  npm test -- --maxWorkers="$VITEST_WORKERS" || fail "frontend: test失敗"
}

frontend_audit() {
  cd "$REPO_ROOT/frontend"
  log "frontend: npm audit"
  # high 未満は日常的に出入りするため止めない。閾値を下げるなら、まず既存の
  # moderate/low を解消してからにする（Issue #83）。
  timeout "$AUDIT_TIMEOUT_SECONDS" npm audit --audit-level=high \
    || fail "frontend: npm audit失敗（依存にhigh以上の既知脆弱性があるか、registryへ到達できません）"
}

frontend_api_schema_freshness() {
  cd "$REPO_ROOT/frontend"
  log "frontend: api-schema.d.tsの鮮度チェック"
  # 失敗経路でも消す。`local` にしない理由も `backend_openapi_freshness` と同じ。
  freshness_tmp="$(mktemp --suffix=.d.ts)"
  trap 'rm -f "$freshness_tmp"' EXIT
  npx openapi-typescript "$REPO_ROOT/backend/openapi.json" -o "$freshness_tmp" \
    || fail "frontend: api-schema.d.tsの生成に失敗しました"
  diff -q src/lib/api-schema.d.ts "$freshness_tmp" >/dev/null \
    || fail "frontend: api-schema.d.tsが最新ではありません（npm run gen:api-typesで再生成してcommitしてください）"
}

# ---- リポジトリ全体のジョブ ----
secret_scan() {
  cd "$REPO_ROOT"
  log "secret検知: detect-secrets"
  # 走査対象は git の追跡下にあるファイルだけにする。detect-secrets 自体の既定も
  # 同じだが、対象を git 側で決めておけば .venv や node_modules を掴む心配がない。
  # baseline に載っている検出は既知として無視され、それ以外が出たときだけ落ちる。
  # baseline の更新手順は CLAUDE.md の「secret検知と依存脆弱性監査」節にある。
  #
  # オプションはどれも外さないこと。
  #   -n  検出した候補を発行元のAPIへ投げて生死を確かめる挙動 (既定で有効) を切る。
  #       付けないと、本物のsecretを踏んだ瞬間にその値が外部サービスへ送られる。
  #   --no-run-if-empty  対象が0件のときに引数無しで起動して緑になるのを防ぐ。
  #   --  以降を全てファイル名として扱わせる。`-h` のような名前のファイルが
  #       追跡下に入るとオプションとして解釈され、走査せず成功してしまう。
  git ls-files -z \
    | xargs -0 --no-run-if-empty \
        uv run --project backend --no-sync detect-secrets-hook --baseline .secrets.baseline -n -- \
    || fail "secret検知: baselineに無いsecretを検出しました（誤検知ならCLAUDE.mdの手順でbaselineを更新してください）"
}

# ---- 依存の用意（並列ジョブの前提になるため直列で済ませる） ----
if [[ -f backend/pyproject.toml ]]; then
  command -v uv >/dev/null 2>&1 || fail "uv未インストール — https://astral.sh/uv"
  ensure_postgres
  log "backend: uv sync"
  (cd backend && uv sync --extra dev --frozen >/dev/null 2>&1) \
    || (cd backend && uv sync --extra dev >/dev/null 2>&1) \
    || fail "backend: uv sync失敗"
fi

if [[ -f frontend/package.json ]]; then
  command -v npm >/dev/null 2>&1 || fail "npm未インストール"
  [[ -d frontend/node_modules ]] || {
    log "frontend: npm ci"
    (cd frontend && npm ci >/dev/null 2>&1) || fail "frontend: npm ci失敗"
  }
fi

# ---- 本体（ここから並列） ----
if [[ -f backend/pyproject.toml ]]; then
  start_job "backend: ruff / ty" backend_lint
  start_job "backend: pytest" backend_pytest
  start_job "backend: uv audit" backend_audit
  start_job "backend: openapi.jsonの鮮度" backend_openapi_freshness
fi

if [[ -f frontend/package.json ]]; then
  start_job "frontend: eslint" frontend_eslint
  start_job "frontend: tsc" frontend_typecheck
  start_job "frontend: vitest" frontend_vitest
  start_job "frontend: npm audit" frontend_audit
  start_job "frontend: api-schema.d.tsの鮮度" frontend_api_schema_freshness
fi

# secret検知だけはリポジトリ全体を対象にするため、backend / frontend のどちらの
# ブロックにも入れない。実体 (detect-secrets) は backend の dev 依存なので backend
# が無ければ走らせようがないが、そのときに黙って省略すると「全チェック緑」が
# 「secretを検査した」を意味しなくなる。省略ではなく失敗させる。
if [[ -f backend/pyproject.toml ]]; then
  start_job "secret検知" secret_scan
else
  fail "secret検知: backend/pyproject.tomlが無く、detect-secretsを実行できません"
fi

log "${#JOB_PIDS[@]}件のチェックを並列実行します（出力は完了後にまとめて表示します）"
wait_jobs

log "PASS: 全チェック緑"
