#!/usr/bin/env bash
# backend / frontend の lint・format・型チェック・テストを一括実行する。
# commit 前に pre-bash-guard.sh から強制実行される。
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

# ---- backend (Python / uv) ----
if [[ -f backend/pyproject.toml ]]; then
  command -v uv >/dev/null 2>&1 || fail "uv未インストール — https://astral.sh/uv"
  ensure_postgres
  pushd backend >/dev/null

  log "backend: uv sync"
  uv sync --extra dev --frozen >/dev/null 2>&1 || uv sync --extra dev >/dev/null 2>&1 \
    || fail "backend: uv sync失敗"

  log "backend: ruff check"
  uv run ruff check . || fail "backend: ruff check失敗"

  log "backend: ruff format --check"
  uv run ruff format --check . || fail "backend: ruff format --check失敗"

  log "backend: ty check"
  uv run ty check || fail "backend: ty check失敗"

  log "backend: pytest"
  uv run pytest || fail "backend: pytest失敗"

  log "backend: openapi.jsonの鮮度チェック"
  OPENAPI_TMP="$(mktemp --suffix=.json)"
  uv run python -m techradar.openapi_export "$OPENAPI_TMP" \
    || fail "backend: openapi.jsonの生成に失敗しました"
  diff -q openapi.json "$OPENAPI_TMP" >/dev/null \
    || fail "backend: openapi.jsonが最新ではありません（uv run python -m techradar.openapi_exportで再生成してcommitしてください）"
  rm -f "$OPENAPI_TMP"

  popd >/dev/null
fi

# ---- frontend (TypeScript / npm) ----
if [[ -f frontend/package.json ]]; then
  command -v npm >/dev/null 2>&1 || fail "npm未インストール"
  pushd frontend >/dev/null

  [[ -d node_modules ]] || { log "frontend: npm ci"; npm ci >/dev/null 2>&1 || fail "frontend: npm ci失敗"; }

  log "frontend: eslint"
  npm run lint || fail "frontend: lint失敗"

  log "frontend: tsc --noEmit"
  npm run typecheck || fail "frontend: typecheck失敗"

  log "frontend: vitest"
  npm test || fail "frontend: test失敗"

  log "frontend: api-schema.d.tsの鮮度チェック"
  API_SCHEMA_TMP="$(mktemp --suffix=.d.ts)"
  npx openapi-typescript "$REPO_ROOT/backend/openapi.json" -o "$API_SCHEMA_TMP" \
    || fail "frontend: api-schema.d.tsの生成に失敗しました"
  diff -q src/lib/api-schema.d.ts "$API_SCHEMA_TMP" >/dev/null \
    || fail "frontend: api-schema.d.tsが最新ではありません（npm run gen:api-typesで再生成してcommitしてください）"
  rm -f "$API_SCHEMA_TMP"

  popd >/dev/null
fi

log "PASS: 全チェック緑"
