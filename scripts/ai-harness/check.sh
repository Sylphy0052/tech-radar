#!/usr/bin/env bash
# backend / frontend の lint・format・型チェック・テストを一括実行する。
# commit 前に pre-bash-guard.sh から強制実行される。
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
log() { printf '[check] %s\n' "$*" >&2; }
fail() { printf '[check][FAIL] %s\n' "$*" >&2; exit 1; }

# ---- backend (Python / uv) ----
if [[ -f backend/pyproject.toml ]]; then
  command -v uv >/dev/null 2>&1 || fail "uv未インストール — https://astral.sh/uv"
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

  popd >/dev/null
fi

log "PASS: 全チェック緑"
