#!/usr/bin/env bash
# TechRadar をローカルで起動する。
#
#   ./run.sh          backend と frontend を起動する
#   ./run.sh --stop   PostgreSQL コンテナも含めて停止する
#
# 常駐するのは PostgreSQL コンテナのみ。backend / frontend は Ctrl-C で終了する。
# ジョブワーカーは backend プロセスに同居するため、別プロセスの起動は不要。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="infra/docker-compose.yml"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
PG_READY_TIMEOUT_SECONDS=60

log() { printf '[run] %s\n' "$*" >&2; }
fail() { printf '[run][FAIL] %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--stop" ]]; then
  log "PostgreSQL を停止します"
  docker compose -f "$COMPOSE_FILE" down
  exit 0
fi

[[ -f .env ]] || { log ".env が無いため .env.example からコピーします"; cp .env.example .env; }

# .env をエクスポートして docker compose と各プロセスへ渡す。
set -a
# shellcheck disable=SC1091
source .env
set +a

command -v docker >/dev/null 2>&1 || fail "docker未インストール"
command -v uv >/dev/null 2>&1 || fail "uv未インストール — https://astral.sh/uv"
command -v npm >/dev/null 2>&1 || fail "npm未インストール"

log "PostgreSQL (pgvector) を起動します"
docker compose -f "$COMPOSE_FILE" up -d postgres

log "PostgreSQL の起動を待機します"
# コンテナ内の pg_isready はホスト側 TCP の受付可否までは保証しないため、
# アプリと同じ経路 (localhost:5432) で接続できるまで待つ。
postgres_accepts_connections() {
  docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready \
    -U "${POSTGRES_USER:-techradar}" -d "${POSTGRES_DB:-techradar}" >/dev/null 2>&1 \
    && (exec 3<>"/dev/tcp/${POSTGRES_HOST:-localhost}/${POSTGRES_PORT:-5432}") 2>/dev/null
}

deadline=$((SECONDS + PG_READY_TIMEOUT_SECONDS))
until postgres_accepts_connections; do
  ((SECONDS < deadline)) \
    || fail "PostgreSQL が ${PG_READY_TIMEOUT_SECONDS} 秒以内に接続を受け付けませんでした"
  sleep 1
done
log "PostgreSQL 起動完了"

log "backend の依存関係を同期します"
(cd backend && uv sync --extra dev >/dev/null) || fail "backend: uv sync失敗"

log "マイグレーションを適用します"
(cd backend && uv run alembic upgrade head) || fail "マイグレーション失敗"

# 公式ソースレジストリの初期データを投入する (冪等)。
# 手動確認済み (verified) の行は上書きしない。
log "公式ソースレジストリを投入します"
(cd backend && uv run python -m techradar.sources.seed) || fail "レジストリ投入失敗"

if [[ ! -d frontend/node_modules ]]; then
  log "frontend の依存関係をインストールします"
  (cd frontend && npm ci) || fail "frontend: npm ci失敗"
fi

pids=()
cleanup() {
  log "停止します (PostgreSQL は起動したままです。完全に停止するには ./run.sh --stop)"
  for pid in "${pids[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

log "backend を起動します (http://localhost:${BACKEND_PORT})"
(cd backend && uv run uvicorn techradar.main:app --reload --port "$BACKEND_PORT") &
pids+=($!)

log "frontend を起動します (http://localhost:${FRONTEND_PORT})"
(cd frontend && npm run dev -- --port "$FRONTEND_PORT") &
pids+=($!)

log "起動完了。Ctrl-C で停止します"
wait
