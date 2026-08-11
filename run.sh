#!/usr/bin/env bash
# TechRadar をローカルで起動する。
#
#   ./run.sh          backend と frontend を起動する
#   ./run.sh --stop   PostgreSQL コンテナも含めて停止する
#
# 常駐するのは PostgreSQL コンテナのみ。backend / frontend は Ctrl-C で終了する。
# ジョブワーカーは backend プロセスに同居するため、別プロセスの起動は不要。
#
# PostgreSQL は既に応答していれば docker に触れずそのまま使う（Issue #55）。
# infra/docker-compose.yml の定義やイメージを変えたときは、`./run.sh --stop` で
# 一度落としてから起動し直さないと反映されない。
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE="infra/docker-compose.yml"
# 既定ポートは 5 桁にする。よく使われる 8000 / 3000 は他プロジェクトのコンテナ等と
# 衝突しやすい。ephemeral port range (32768-60999) の外から選び、OS が一時ポートとして
# 割り当てて散発的に衝突することも避ける。変更する場合は CORS_ALLOW_ORIGINS と
# NEXT_PUBLIC_API_BASE_URL (.env) も揃えること。
BACKEND_PORT="${BACKEND_PORT:-18700}"
FRONTEND_PORT="${FRONTEND_PORT:-13700}"

# listen するインターフェース。単一ユーザーがローカルで動かす前提のため、既定では
# 他の端末から届かない 127.0.0.1 に閉じる（Issue #64）。認証を置いていないので
# （PROJECT_SPEC.md §18）、届いた時点で中身が見えてしまう。
#
# uvicorn は --host の既定が 127.0.0.1 だが、next dev は --hostname (-H) を
# 渡さないと全インターフェースへ bind する。既定に任せると両者で範囲が食い違うため、
# どちらにも明示して渡す。
BIND_HOST="${BIND_HOST:-127.0.0.1}"

log() { printf '[run] %s\n' "$*" >&2; }
fail() { printf '[run][FAIL] %s\n' "$*" >&2; exit 1; }

# docker へ到達できないときの案内へ埋め込む。check.sh と共有するライブラリ側からは
# 呼び出し元のパスも引数も分からないため、ここで渡す。引数まで渡すのは、`--stop` の
# 案内が `./run.sh` になってしまうと、そのまま実行したときに停止ではなく起動して
# しまうため。
ENTRYPOINT_SCRIPT="${BASH_SOURCE[0]}"
ENTRYPOINT_ARGS="$*"

# PostgreSQL の起動確認は check.sh と共有する（Issue #55）。
# shellcheck source=scripts/ai-harness/lib/postgres.sh
source "$REPO_ROOT/scripts/ai-harness/lib/postgres.sh"

# 前回の起動を止めるための関数群。
# shellcheck source=scripts/ai-harness/lib/app_processes.sh
source "$REPO_ROOT/scripts/ai-harness/lib/app_processes.sh"

# 起動したプロセスグループの ID を残す場所。git 管理外（.gitignore 済み）。
# 次回の起動で「前回の残り」を特定するために使う。
BACKEND_PID_FILE="$REPO_ROOT/.run/backend.pid"
FRONTEND_PID_FILE="$REPO_ROOT/.run/frontend.pid"

if [[ "${1:-}" == "--stop" ]]; then
  log "PostgreSQL を停止します"
  assert_docker_usable "PostgreSQLの停止"
  docker compose -f "$COMPOSE_FILE" down
  exit 0
fi

[[ -f .env ]] || { log ".env が無いため .env.example からコピーします"; cp .env.example .env; }

# .env をエクスポートして docker compose と各プロセスへ渡す。
set -a
# shellcheck disable=SC1091
source .env
set +a

# 設定ファイルを読んだ後に確かめる。空のまま渡すと next dev が既定へ落ちて全
# インターフェースへ開くため、既定値へ戻さず止める（Issue #64）。空白だけの値も
# 同じ扱いにする。タブや改行が紛れた場合は起動が失敗するだけだが、原因が分かる
# ところで止めたい。
[[ "$BIND_HOST" =~ [^[:space:]] ]] || fail "BIND_HOSTが空です（閉じた既定は 127.0.0.1）"

# docker は PostgreSQL を実際に起動するときだけ要る。既に動いていれば触らない。
command -v uv >/dev/null 2>&1 || fail "uv未インストール — https://astral.sh/uv"
command -v npm >/dev/null 2>&1 || fail "npm未インストール"

ensure_postgres

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

# 前回の起動が残っていれば先に止める。同じポートを掴んだままの相手が居ると、
# uvicorn / next dev はエラーで落ちるか、こちらが起動したつもりで前回のプロセスを
# 見続けることになる。判定は PID ファイルとコマンドラインの二重で行う。
stop_previous_instance "$BACKEND_PID_FILE" "backend" \
  "uvicorn techradar.main:app" "--port $BACKEND_PORT"
stop_previous_instance "$FRONTEND_PID_FILE" "frontend" \
  "next dev" "--port $FRONTEND_PORT"

pids=()
cleanup() {
  log "停止します (PostgreSQL は起動したままです。完全に停止するには ./run.sh --stop)"
  for pid in "${pids[@]:-}"; do
    [[ -n "$pid" ]] || continue
    # 起動時に独立したプロセスグループを作っているため、グループごと止める。
    # プロセス単体へ送ると、uvicorn の reload 用の子や next-server が残る。
    stop_process_group "$pid" || true
  done
  # 自分が書いた PID ファイルだけを消す。新しい実行に止められた場合、この後片付けは
  # 相手が自分の値を書き終えた後に走るため、無条件に消すと相手の分まで消えてしまう。
  remove_pid_file_if_matches "$BACKEND_PID_FILE" "${backend_pgid:-}"
  remove_pid_file_if_matches "$FRONTEND_PID_FILE" "${frontend_pgid:-}"
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 起動コマンドへは環境変数として渡す。値を二重引用符で組み立てて渡すと、引用符を含む
# 設定値でコマンドの構造が変わってしまう。
export BIND_HOST BACKEND_PORT FRONTEND_PORT

log "backend を起動します (http://${BIND_HOST}:${BACKEND_PORT})"
# setsid で独立したプロセスグループにする。子孫までまとめて止められるようにするため。
# Ctrl-C の SIGINT は届かなくなるが、停止は上の cleanup が担う。
# 変数はここではなく起動先のシェルで展開する（上の export 参照）。
# shellcheck disable=SC2016
setsid bash -c 'cd backend && exec uv run uvicorn techradar.main:app --reload --host "$BIND_HOST" --port "$BACKEND_PORT"' &
backend_pgid="$(process_group_of $!)"
pids+=("$backend_pgid")
write_pid_file "$BACKEND_PID_FILE" "$backend_pgid"

log "frontend を起動します (http://${BIND_HOST}:${FRONTEND_PORT})"
# shellcheck disable=SC2016
setsid bash -c 'cd frontend && exec npm run dev -- --hostname "$BIND_HOST" --port "$FRONTEND_PORT"' &
frontend_pgid="$(process_group_of $!)"
pids+=("$frontend_pgid")
write_pid_file "$FRONTEND_PID_FILE" "$frontend_pgid"

log "起動完了。Ctrl-C で停止します"
wait
