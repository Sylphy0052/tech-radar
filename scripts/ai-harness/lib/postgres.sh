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
# 設定ファイルの場所。呼び出し元はリポジトリルートで実行する前提。
: "${ENV_FILE:=.env}"

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

# 前後の空白を落とす。
trim_spaces() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

# 打ち切りの上限（バイト）。docker のエラー出力は通常これに収まる。小さくしすぎると
# 原因を含む行が消えて一次情報として使えなくなるため、余裕を持たせる。
_MESSAGE_VALUE_MAX_BYTES=2000
# 打ち切ったときに末尾へ足す。切ったことを隠すと「これで全部」と読まれる。
_MESSAGE_VALUE_ELLIPSIS='…（以下省略）'

# 外部コマンドの出力をメッセージへ埋める前に通す（Issue #72）。
#
# メッセージはそのまま端末へ出る。端末制御シーケンスが混じっていれば、画面を消す・
# カーソルを戻して別の文言に見せる・ウィンドウタイトルを書き換える、といった形で表示を
# 攪乱できる。落ちた理由を判断するための一次情報が攪乱されると読み手が判断を誤る。
# docker バイナリの差し替えかデーモンの応答の細工が要るため脅威モデルとしては外側だが、
# 防御の深さとして埋めておく。
#
# 落とすのは制御文字であってシーケンス全体ではない。`ESC [ 2 J` を渡すと `[2J` が文字
# として残る。ESC を失えば端末はこれを制御として解釈しないため、残骸が出ても攪乱には
# 繋がらない。タブと改行は残す（複数行のエラーをそのまま読めるように）。
#
# C1 制御文字（`\200-\237`）は落とさない。UTF-8 の継続バイトと範囲が重なり、日本語を
# 含む出力を壊すため。ここは前提を置いている: 端末が UTF-8 で読んでいる限り、この範囲の
# バイトは文字の一部にしかならない。8bit の制御コードを解釈する設定の端末（非UTF-8の
# ロケールなど）では、`\233` 単体が ESC [ と同じ働きをする経路が残る。UTF-8 を前提に
# できない環境まで守ろうとすると日本語の出力を壊すため、そちらは見送っている。
#
# 値の比較には使わない。整形した値で突き合わせると、制御文字を挟んだ値が一致してしまい
# 判定が緩む（`warn_if_published_host_differs` の公開先の照合がこれに当たる）。
sanitize_for_message() {
  local value="$1" cleaned cut
  # 文字クラスの範囲指定は照合順序に左右されるため固定する（Issue #71 と同じ轍）。
  # `-x` を付けるのは `tr` へも渡すため。長さの数え方もバイトで揃う。関数を抜ければ戻る。
  # 呼び出し元が既に固定していても、この関数だけで呼ばれる経路のために自分で置く。
  local -x LC_ALL=C
  cleaned="$(printf '%s' "$value" | tr -d '\000-\010\013-\037\177')"
  if ((${#cleaned} > _MESSAGE_VALUE_MAX_BYTES)); then
    # 切れ目が多バイト文字の途中へ落ちたら、その文字の手前まで戻す。途中で切ると
    # 壊れたバイト列が出て、C1 を残した意味（日本語をそのまま読める）が無くなる。
    # UTF-8 の継続バイトは `\200-\277` で、1文字あたり最大3個しか続かない。
    cut=$_MESSAGE_VALUE_MAX_BYTES
    while ((cut > 0)) && [[ "${cleaned:cut:1}" == [$'\200'-$'\277'] ]]; do
      ((cut--))
    done
    cleaned="${cleaned:0:cut}${_MESSAGE_VALUE_ELLIPSIS}"
  fi
  printf '%s' "$cleaned"
}

# 設定ファイルから `BIND_HOST` の値だけを拾う。読めなければ空を返す。
#
# `source` せずに拾うのは、呼び出し元の環境を書き換えないため。ファイルが無い場合に
# ここで落ちないよう、失敗はすべて空として扱う（worktree を作った直後は設定ファイルが
# まだ無く、`set -e` の下でそのまま落とすと起動確認どころではなくなる）。
#
# 読めるのは 1 行 1 代入の `BIND_HOST=<値>` だけ。`export` 付き・前後のクォート・
# 空白を挟んだ行末コメント・CRLF は落とす。`FOO=bar; BIND_HOST=...` のように 1 行へ
# 詰めた形は読まない（既定値へ落ちる）。`docker compose` の解決結果を使わないのは、
# compose が読むのは `infra/` 側の設定であり、`run.sh` が渡す値とは別物になるため。
read_bind_host_from_env_file() {
  local raw=""
  [[ -f "$ENV_FILE" ]] || return 0
  raw="$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?BIND_HOST[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" 2>/dev/null | tail -1)" || return 0
  raw="${raw%%#*}"
  raw="${raw%$'\r'}"
  raw="$(trim_spaces "$raw")"
  raw="${raw#[\"\']}"
  raw="${raw%[\"\']}"
  trim_spaces "$raw"
}

# 期待する公開先を返す。環境にあればそれ、無ければ設定ファイルから拾う。
#
# `run.sh` は設定ファイルを読んでから呼ぶが、`check.sh` は読まない。環境変数だけを見ると、
# 設定ファイルで広げている運用に対して `check.sh` から呼ぶたび食い違い扱いになり、
# 正しい状態に警告が出続ける。両方から同じ値を見るためにここで拾う。
expected_bind_host() {
  local value="${BIND_HOST:-}"
  [[ "$value" =~ [^[:space:]] ]] || value="$(read_bind_host_from_env_file)"
  [[ "$value" =~ [^[:space:]] ]] || value="127.0.0.1"
  trim_spaces "$value"
}

# 起動中のコンテナが、いま設定している範囲へ公開されているかを見る（Issue #65）。
#
# `docker compose` の ports を変えても、既に動いているコンテナは作り直すまで古い範囲の
# ままになる。閉じたつもりが LAN へ開いたまま、という状態に気付けないため警告を出す。
#
# 見られる範囲には限りがある。docker へ触れないシェルや、compose を通さず立てた
# PostgreSQL、別のプロジェクト名で起動したコンテナは判定できない。確かめられなかった
# ことも黙らず出す。「警告が出ない＝閉じている」と読まれると、この処理が無いときより
# 危うくなるため。
warn_if_published_host_differs() {
  local expected published entry
  expected="$(expected_bind_host)"

  if ! docker_is_reachable; then
    log "PostgreSQL の公開先は確認できませんでした（dockerへ到達できないため）"
    return 0
  fi

  # URL だけを1行ずつ取り出して完全一致で見る。まとめて部分一致にすると、
  # 127.0.0.1 が 127.0.0.10 に一致するような取りこぼしが起きる。
  published="$(docker compose -f "$COMPOSE_FILE" ps --format '{{range .Publishers}}{{.URL}}
{{end}}' postgres 2>/dev/null)" || published=""

  if [[ -z "${published//[[:space:]]/}" ]]; then
    log "PostgreSQL の公開先は確認できませんでした（compose から取得できないため）"
    return 0
  fi

  while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    [[ "$entry" == "$expected" ]] && continue
    # 照合は生の値で済ませ、整形するのは表示だけ（`sanitize_for_message` の注記を参照）。
    log "警告: 起動中の PostgreSQL が $(sanitize_for_message "$entry") へ公開されています（設定は $(sanitize_for_message "$expected")）"
    log "  ./run.sh --stop で落としてから ./run.sh で起動し直すと反映されます（Issue #65）"
    return 0
  done <<<"$published"
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

# 一度確かめたら覚えておく。起動確認の経路で複数回聞かれるが、`docker info` は
# 100ms 程度かかるうえ、同じプロセスの中で到達性が変わることはない。
docker_is_reachable() {
  case "${_DOCKER_REACHABLE:-}" in
    yes) return 0 ;;
    no) return 1 ;;
  esac
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    _DOCKER_REACHABLE=yes
    return 0
  fi
  _DOCKER_REACHABLE=no
  return 1
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
# 案内へそのまま載せてよい文字。英数字と `. _ / -` と半角空白だけを通す。
#
# 案内は `sg docker -c <コマンド>` の形で示すが、`sg` は受け取った文字列を
# `/bin/sh -c` で実行する（man sg）。つまり引用をどれだけ正しく付けても、中身は
# シェルへ渡ってから改めてコマンドとして解釈される。`;` や `$(...)` が混じっていれば、
# 案内をコピーして実行した人の手元で docker group（root相当）の権限のまま動く。
#
# 危険な文字を数え上げる方式は漏れがそのまま穴になるため、通す側を並べる。判定から
# 漏れた文字は「案内を出さない」側へ倒れる（Issue #71）。`=` や `:` のように、それ自体は
# 安全でも通していない文字がある。増やすには、その文字が `/bin/sh` にとって何でもない
# ことを確かめる必要があるため、実際に要るまで広げない。
#
# 非ASCIIの文字も通さない。日本語を含むパスから起動した場合は一行の案内が出ず、
# 入り直す手順だけの案内になる。
# 定数として扱う。`readonly` にはしない（このファイルを同じシェルで2回読み込むと
# 再代入で落ちるため。読み込みが1回で済む今の呼び出し方に依存させない）。
_SAFE_COMMAND_HINT_PATTERN='^[A-Za-z0-9._/ -]+$'

assert_docker_reachable() {
  local error command_hint retry_hint shown_error
  # 文字クラスの範囲指定（`A-Z` など）は照合順序に左右されるため、判定の間はロケールを
  # 固定する。`-x` を付けるのは `docker` にも渡すため。`local` だけでは、呼び出し元が
  # 既に `LC_ALL` を export している場合しか子プロセスへ伝わらない。docker のメッセージが
  # 英語で揃えば `permission denied` の判定も安定する。関数を抜ければ元へ戻る。
  local -x LC_ALL=C
  error="$(docker info 2>&1 >/dev/null)" && return 0
  # 案内をそのまま実行できるよう、呼び出し元スクリプトのパスと引数を使う。ここで
  # `${BASH_SOURCE[0]}` を見るとこのライブラリ自身のパスになってしまう。引数を
  # 落とすと `./run.sh --stop` の案内が `./run.sh` になり、そのまま実行すると
  # 停止のつもりで起動してしまう。
  command_hint="${ENTRYPOINT_SCRIPT:-$0}"
  [[ -n "${ENTRYPOINT_ARGS:-}" ]] && command_hint="${command_hint} ${ENTRYPOINT_ARGS}"

  # 出し分けの判定は生の出力で行い、メッセージへ埋めるのは整形した方（Issue #72）。
  shown_error="$(sanitize_for_message "$error")"

  if [[ "$error" == *"permission denied"* ]]; then
    # そのまま実行できる一行を出せるのは、中身が上のパターンに収まるときだけ。
    # `${var@Q}` で引用するのは、案内文を読むシェルが1語として受け取れるようにするため。
    # 引用だけでは `sg` の内側でのコマンド解釈は止められないので、両方が要る。
    if [[ "$command_hint" =~ $_SAFE_COMMAND_HINT_PATTERN ]]; then
      retry_hint="sg docker -c ${command_hint@Q} のように実行してください"
    else
      # 呼び出し元のパスと引数のどちらが原因かは分けずに書く。パス側に特殊な文字が
      # 入ることもあり、「引数に」と断じると原因を取り違えさせる。
      retry_hint="sg dockerで入り直してから元のコマンドを実行し直してください（実行しようとしたコマンドに特殊な文字が含まれるため、そのまま実行できる形では示しません）"
    fi
    fail "dockerへ接続できません — docker groupが現在のシェルに反映されていない可能性があります。newgrp dockerで入り直すか、${retry_hint}（docker groupはroot相当の権限を持ちます）。dockerの出力: ${shown_error}"
  fi
  fail "dockerへ接続できません — dockerの出力: ${shown_error}"
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
