#!/usr/bin/env bash
# backend / frontend の lint・format・型チェック・テストを一括実行する。
# commit 前に pre-bash-guard.sh から強制実行される。
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

# pytest を何プロセスへ分散させるか（pytest-xdist）。ワーカーごとにテスト用 DB を
# 作り直すため（Issue #33）、CPU 数ぶんまで増やすと PostgreSQL 側が重くなり、
# 他のチェックと CPU も取り合う。既定値は実測で決めた（Issue #61）。
# 1 を指定すると xdist を使わず単一プロセスで実行する。
PYTEST_WORKERS="${PYTEST_WORKERS:-8}"

# vitest のワーカー数。既定では CPU 数ぶんまで起動するため、pytest と同時に走ると
# コア数を超えて奪い合い、両方とも単独実行より遅くなる（実測）。上限を決めておく。
VITEST_WORKERS="${VITEST_WORKERS:-8}"

# 並列実行するジョブを、ラベル・PID・出力先の3つの並びで管理する。
JOB_LABELS=()
JOB_PIDS=()
JOB_LOGS=()

cleanup_job_logs() {
  ((${#JOB_LOGS[@]} > 0)) && rm -f "${JOB_LOGS[@]}"
  return 0
}
trap cleanup_job_logs EXIT

# ジョブをバックグラウンドで開始する。出力は端末へ流さずファイルへ溜める。
start_job() {
  local label="$1"
  shift
  local logfile
  logfile="$(mktemp)"
  ("$@") >"$logfile" 2>&1 &
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

backend_openapi_freshness() {
  cd "$REPO_ROOT/backend"
  log "backend: openapi.jsonの鮮度チェック"
  local tmp
  tmp="$(mktemp --suffix=.json)"
  uv run python -m techradar.openapi_export "$tmp" \
    || fail "backend: openapi.jsonの生成に失敗しました"
  diff -q openapi.json "$tmp" >/dev/null \
    || fail "backend: openapi.jsonが最新ではありません（uv run python -m techradar.openapi_exportで再生成してcommitしてください）"
  rm -f "$tmp"
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

frontend_api_schema_freshness() {
  cd "$REPO_ROOT/frontend"
  log "frontend: api-schema.d.tsの鮮度チェック"
  local tmp
  tmp="$(mktemp --suffix=.d.ts)"
  npx openapi-typescript "$REPO_ROOT/backend/openapi.json" -o "$tmp" \
    || fail "frontend: api-schema.d.tsの生成に失敗しました"
  diff -q src/lib/api-schema.d.ts "$tmp" >/dev/null \
    || fail "frontend: api-schema.d.tsが最新ではありません（npm run gen:api-typesで再生成してcommitしてください）"
  rm -f "$tmp"
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
  start_job "backend: openapi.jsonの鮮度" backend_openapi_freshness
fi

if [[ -f frontend/package.json ]]; then
  start_job "frontend: eslint" frontend_eslint
  start_job "frontend: tsc" frontend_typecheck
  start_job "frontend: vitest" frontend_vitest
  start_job "frontend: api-schema.d.tsの鮮度" frontend_api_schema_freshness
fi

log "${#JOB_PIDS[@]}件のチェックを並列実行します（出力は完了後にまとめて表示します）"
wait_jobs

log "PASS: 全チェック緑"
