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
ensure_postgres() {
  [[ "${CI:-}" == "true" ]] && { log "CI環境のためPostgreSQL起動をskip"; return 0; }
  [[ -f "$COMPOSE_FILE" ]] || return 0

  local host="${POSTGRES_HOST:-localhost}" port="${POSTGRES_PORT:-5432}"

  # 起動確認は docker を経由せず TCP で行う。テストが実際に使う経路そのものであり、
  # docker へ触れないシェル (docker group が未反映など) でも判定できる。
  if postgres_port_is_open "$host" "$port"; then
    return 0
  fi

  # ここから先は起動が必要で、docker が要る。
  command -v docker >/dev/null 2>&1 || fail "docker未インストール — 結合テストにPostgreSQLが必要"
  assert_docker_reachable

  log "PostgreSQL を起動します"
  docker compose -f "$COMPOSE_FILE" up -d postgres >/dev/null 2>&1 \
    || fail "PostgreSQL の起動に失敗しました"

  # 自分で起動した直後は初期化中がありうる。コンテナ内の pg_isready はホスト側 TCP の
  # 受付可否までは保証しないため、両方を確認する。
  local deadline=$((SECONDS + PG_READY_TIMEOUT_SECONDS))
  until postgres_is_ready "$host" "$port"; do
    ((SECONDS < deadline)) \
      || fail "PostgreSQL が ${PG_READY_TIMEOUT_SECONDS} 秒以内に接続を受け付けませんでした"
    sleep 1
  done
  log "PostgreSQL 起動完了"
}

# docker デーモンへ到達できるかを確認する。
# 権限で弾かれるのは docker group が現在のシェルへ反映されていないときが多く、
# 素の失敗メッセージからは切り分けられないため、対処まで示して落とす。
assert_docker_reachable() {
  local error
  error="$(docker info 2>&1 >/dev/null)" && return 0

  if [[ "$error" == *"permission denied"* ]]; then
    fail "dockerへ接続できません — docker group が現在のシェルに反映されていない可能性があります。
      newgrp docker で入り直すか、sg docker -c \"$0\" のように実行してください。
      docker の出力: ${error}"
  fi
  fail "dockerへ接続できません — docker の出力: ${error}"
}

postgres_port_is_open() {
  local host="$1" port="$2"
  (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null
}

postgres_is_ready() {
  local host="$1" port="$2"
  docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready \
    -U "${POSTGRES_USER:-techradar}" -d "${POSTGRES_DB:-techradar}" >/dev/null 2>&1 \
    && postgres_port_is_open "$host" "$port"
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
