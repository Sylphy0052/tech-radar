#!/usr/bin/env bash
# backend / frontend の lint・format・型チェック・テストを一括実行する。
# commit 前に pre-bash-guard.sh から強制実行される。
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"
log() { printf '[check] %s\n' "$*" >&2; }
fail() { printf '[check][FAIL] %s\n' "$*" >&2; exit 1; }

COMPOSE_FILE="infra/docker-compose.yml"
PG_READY_TIMEOUT_SECONDS=60

# DB を使う結合テストのため PostgreSQL を起動する。
# 既に起動していれば何もしない。CI では services で提供されるため skip する。
#
# コンテナ内の pg_isready はホスト側 TCP の受付可否までは保証しないため、
# テストと同じ経路 (localhost:5432) で接続できるまで待つ。
ensure_postgres() {
  [[ "${CI:-}" == "true" ]] && { log "CI環境のためPostgreSQL起動をskip"; return 0; }
  [[ -f "$COMPOSE_FILE" ]] || return 0
  command -v docker >/dev/null 2>&1 || fail "docker未インストール — 結合テストにPostgreSQLが必要"

  local host="${POSTGRES_HOST:-localhost}" port="${POSTGRES_PORT:-5432}"
  if postgres_accepts_connections "$host" "$port"; then
    return 0
  fi

  log "PostgreSQL を起動します"
  docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null 2>&1 \
    || fail "PostgreSQL の起動に失敗しました"

  local deadline=$((SECONDS + PG_READY_TIMEOUT_SECONDS))
  until postgres_accepts_connections "$host" "$port"; do
    ((SECONDS < deadline)) \
      || fail "PostgreSQL が ${PG_READY_TIMEOUT_SECONDS} 秒以内に接続を受け付けませんでした"
    sleep 1
  done
  log "PostgreSQL 起動完了"
}

postgres_accepts_connections() {
  local host="$1" port="$2"
  docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready \
    -U "${POSTGRES_USER:-techradar}" -d "${POSTGRES_DB:-techradar}" >/dev/null 2>&1 \
    && (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null
}

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
