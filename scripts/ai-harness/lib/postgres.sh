#!/usr/bin/env bash
# PostgreSQL の起動確認を check.sh と run.sh で共有する。
#
# 同じ判定を両方が別々に持っていたため、docker group が現在のシェルへ反映されて
# いない場合の手当てが check.sh にだけ入り、run.sh は素の権限エラーで落ちる状態が
# 残った（Issue #52 → #55）。判定を一箇所に集めて同じ事故を防ぐ。
#
# 呼び出し元が先に用意しておくもの:
#
# - `log` / `fail` — メッセージのプレフィックスは呼び出し元ごとに異なるため、
#   このファイルでは定義せず呼び出し元のものを使う
# - `COMPOSE_FILE` — docker compose の定義ファイルへのパス
# - `ENTRYPOINT_SCRIPT` — 呼び出し元スクリプト自身のパス。docker へ到達できない
#   ときの案内文へ埋め込み、そのまま実行できる形で示すために使う
# - `ENTRYPOINT_ARGS` — 呼び出し元が受け取った引数（任意）。案内文へ一緒に埋め込む。
#   `./run.sh --stop` のように引数で挙動が変わる場合、これが無いと案内どおり実行した
#   ときに別の動作をしてしまう

# 呼び出し元が別の値を決めているならそれを尊重する。
: "${PG_READY_TIMEOUT_SECONDS:=60}"
# 1 回の TCP 疎通確認に許す秒数。ローカルの localhost 相手なら一瞬で終わる。
: "${PG_PORT_PROBE_TIMEOUT_SECONDS:=3}"

# PostgreSQL を使えるようにする。既に起動していれば何もしない。
# `CI=true` のときは何もせず返す。CI では services が PostgreSQL を提供するため。
# この分岐は呼び出し元によらず効くので、ローカル用のスクリプトから呼ぶ場合も
# `CI` が紛れ込んでいないか気に留めること。
ensure_postgres() {
  [[ "${CI:-}" == "true" ]] && { log "CI環境のためPostgreSQL起動をskip"; return 0; }
  [[ -f "$COMPOSE_FILE" ]] || return 0

  # ここで見るホストとポートは、アプリやテストが `DATABASE_URL` で接続する先と同じで
  # ある前提。既定値どうしなら一致する。片方だけ環境変数で差し替えると、起動確認と
  # 実際の接続先が食い違い、確認は通るのに接続エラーで落ちる。
  local host="${POSTGRES_HOST:-localhost}" port="${POSTGRES_PORT:-5432}"

  if postgres_looks_available "$host" "$port"; then
    warn_if_published_host_differs
    return 0
  fi

  # ここから先は起動が必要で、docker が要る。
  assert_docker_usable "PostgreSQLの起動"

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

# 起動中のコンテナが、いま設定している範囲へ公開されているかを見る（Issue #65）。
#
# `docker compose` の ports を変えても、既に動いているコンテナは作り直すまで古い範囲の
# ままになる。閉じたつもりが LAN へ開いたまま、という状態に気付けないため警告を出す。
# 判定できないときは黙って返す。ここは起動の可否とは無関係で、docker へ触れないシェル
# や別の手段で立てた PostgreSQL を相手にしていることもある。
warn_if_published_host_differs() {
  local expected="${BIND_HOST:-127.0.0.1}" published
  docker_is_reachable || return 0
  published="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Publishers}}' postgres 2>/dev/null)" || return 0
  [[ -n "$published" ]] || return 0
  [[ "$published" == *"$expected"* ]] && return 0
  log "警告: 起動中の PostgreSQL の公開先が設定 (${expected}) と違います — ${published}"
  log "  反映するには ./run.sh --stop でコンテナを作り直してください（Issue #65）"
}

# 起動済みかどうかを判定する。
# docker が使えるならコンテナ内の pg_isready まで見る。5432 は取り合いになりやすいポートで、
# 別プロセスが掴んでいても TCP は通ってしまうため、確かめられるなら確かめる。
# docker へ触れないシェル (docker group が未反映など) では TCP の結果だけで判断する。
# ここで諦めても、接続先が実は PostgreSQL でなければ後続の処理が接続エラーで落ちる。
postgres_looks_available() {
  local host="$1" port="$2"
  postgres_port_is_open "$host" "$port" || return 1
  docker_is_reachable || return 0
  postgres_is_ready "$host" "$port"
}

docker_is_reachable() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

# docker が無いと先へ進めない場面で、未インストールと到達不可をまとめて判定する。
# `purpose` には「何のために docker が要るのか」を渡す（例: PostgreSQLの起動）。
assert_docker_usable() {
  local purpose="$1"
  command -v docker >/dev/null 2>&1 || fail "docker未インストール — ${purpose}に必要です"
  assert_docker_reachable
}

# docker デーモンへ到達できないまま先へ進めない場面で落とす。
# 権限で弾かれるのは docker group が現在のシェルへ反映されていないときが多く、
# 素の失敗メッセージからは切り分けられないため、対処まで示す。
assert_docker_reachable() {
  local error command_hint
  error="$(docker info 2>&1 >/dev/null)" && return 0
  # 案内をそのまま実行できるよう、呼び出し元スクリプトのパスと引数を使う。ここで
  # `${BASH_SOURCE[0]}` を見るとこのライブラリ自身のパスになってしまう。引数を
  # 落とすと `./run.sh --stop` の案内が `./run.sh` になり、そのまま実行すると
  # 停止のつもりで起動してしまう。
  command_hint="${ENTRYPOINT_SCRIPT:-$0}"
  [[ -n "${ENTRYPOINT_ARGS:-}" ]] && command_hint="${command_hint} ${ENTRYPOINT_ARGS}"

  if [[ "$error" == *"permission denied"* ]]; then
    fail "dockerへ接続できません — docker groupが現在のシェルに反映されていない可能性があります。newgrp dockerで入り直すか、sg docker -c \"${command_hint}\" のように実行してください（docker groupはroot相当の権限を持ちます）。dockerの出力: ${error}"
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
