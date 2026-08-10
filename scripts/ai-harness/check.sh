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
# 1 回の TCP 疎通確認に許す秒数。ローカルの localhost 相手なら一瞬で終わる。
PG_PORT_PROBE_TIMEOUT_SECONDS=3

# DB を使う結合テストのため PostgreSQL を起動する。
# 既に起動していれば何もしない。CI では services で提供されるため skip する。
ensure_postgres() {
  [[ "${CI:-}" == "true" ]] && { log "CI環境のためPostgreSQL起動をskip"; return 0; }
  [[ -f "$COMPOSE_FILE" ]] || return 0

  # ここで見るホストとポートは、テストが `DATABASE_URL` で接続する先と同じである前提。
  # 既定値どうしなら一致する。片方だけ環境変数で差し替えると、起動確認と実際の接続先が
  # 食い違い、確認は通るのにテストが接続エラーで落ちる。
  local host="${POSTGRES_HOST:-localhost}" port="${POSTGRES_PORT:-5432}"

  if postgres_looks_available "$host" "$port"; then
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

# 起動済みかどうかを判定する。
# docker が使えるならコンテナ内の pg_isready まで見る。5432 は取り合いになりやすいポートで、
# 別プロセスが掴んでいても TCP は通ってしまうため、確かめられるなら確かめる。
# docker へ触れないシェル (docker group が未反映など) では TCP の結果だけで判断する。
# ここで諦めても、接続先が実は PostgreSQL でなければ後続の pytest が接続エラーで落ちる。
postgres_looks_available() {
  local host="$1" port="$2"
  postgres_port_is_open "$host" "$port" || return 1
  docker_is_reachable || return 0
  postgres_is_ready "$host" "$port"
}

docker_is_reachable() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

# docker デーモンへ到達できないまま先へ進めない場面で落とす。
# 権限で弾かれるのは docker group が現在のシェルへ反映されていないときが多く、
# 素の失敗メッセージからは切り分けられないため、対処まで示す。
assert_docker_reachable() {
  local error script
  error="$(docker info 2>&1 >/dev/null)" && return 0
  # `$0` は source された場合に呼び出し元のシェル名になる。案内をそのまま実行できるよう
  # スクリプト自身のパスを使う。
  script="${BASH_SOURCE[0]}"

  if [[ "$error" == *"permission denied"* ]]; then
    fail "dockerへ接続できません — docker groupが現在のシェルに反映されていない可能性があります。newgrp dockerで入り直すか、sg docker -c \"${script}\" のように実行してください（docker groupはroot相当の権限を持ちます）。dockerの出力: ${error}"
  fi
  fail "dockerへ接続できません — dockerの出力: ${error}"
}

# 到達不能なホストを指していると OS 既定のタイムアウトまで待たされるため上限を切る。
# host / port は `bash -c` の中へ埋め込まず引数で渡す。埋め込むと `POSTGRES_HOST` の
# 中身がそのままコマンドとして解釈されうるため。
postgres_port_is_open() {
  local host="$1" port="$2"
  timeout "$PG_PORT_PROBE_TIMEOUT_SECONDS" \
    bash -c 'exec 3<>"/dev/tcp/$0/$1"' "$host" "$port" 2>/dev/null
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
