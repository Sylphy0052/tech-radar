#!/usr/bin/env bash
# backend / frontend の起動プロセスを扱う関数群。
#
# `run.sh` は起動のたびに、前回の実行が残っていれば先に止める。止め損ねるとポートを
# 掴んだままの相手と衝突し、止めすぎると無関係のプロセスを殺す。判定を2段構えにして
# どちらも避ける。
#
# 1. 前回の実行が書いた PID ファイル (プロセスグループID) を読む
# 2. その ID のリーダープロセスのコマンドラインが期待どおりかを確かめてから停止する
#
# PID は再利用されるため 1 だけでは足りない。異常終了で PID ファイルが残り、その番号を
# 別のプロセスが使っていれば無関係なものを止めてしまう。逆に 2 だけでも足りない。
# `next dev` は `next-server` という別名の子を持ち、その子のコマンドラインにはポート番号が
# 入らないため、パターン一致では取りこぼす。プロセスグループ単位で止めることで、
# 名前の変わる子孫まで確実に片付ける。
#
# ポート番号から PID を引く方法 (`lsof -ti tcp:<port>` / `ss -lptn`) は使わない。WSL では
# 自分のプロセスであっても空を返す (2026-08-12 実測)。`ps` からは見えるため、
# コマンドラインの一致で判定する。
#
# 関数は `set -euo pipefail` の下で読み込まれても落ちないようにする。止める相手が
# 居ないことは正常な状態であり、起動を中断する理由にはならない。

# 停止時に SIGTERM から SIGKILL へ切り替えるまでの既定の猶予秒数。
APP_PROCESS_STOP_GRACE_SECONDS="${APP_PROCESS_STOP_GRACE_SECONDS:-5}"

# PID ファイルを読む。正の整数が1つだけ書かれていなければ空を返す。
#
# 壊れた値でも失敗させないのは、ここで止まると「前回の残骸のせいで起動できない」
# 状態になるため。読めなければ PID ファイルが無いのと同じ扱いにして先へ進む。
read_pid_file() {
  local path="$1"
  local value

  [[ -f "$path" ]] || return 0
  value="$(<"$path")"
  # 前後の空白と改行だけを落とす。内側の空白まで詰めると "12 34" が "1234" になり、
  # 壊れた値を正しい PID として受け入れてしまう。
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || return 0
  printf '%s' "$value"
}

# PID ファイルを書く。親ディレクトリが無ければ作る。
write_pid_file() {
  local path="$1"
  local pid="$2"

  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$pid" > "$path"
}

# 中身が指定の値と一致するときだけ PID ファイルを消す。
#
# 後から起動した側が自分の値を書いた後に、先に居た側の後片付けが走ることがある
# （新しい実行が古い実行を止めると、古い側の trap がそこで動く）。無条件に消すと、
# 動いているインスタンスの PID ファイルを消してしまう。
remove_pid_file_if_matches() {
  local path="$1"
  local expected="$2"
  local current

  current="$(read_pid_file "$path")"
  [[ "$current" == "$expected" ]] || return 0
  rm -f "$path"
}

# 指定 PID が属するプロセスグループ ID を返す。取得できなければ PID をそのまま返す。
#
# `setsid cmd &` の `$!` は、必ずしもプロセスグループのリーダーにならない。`setsid` は
# 自身が既にプロセスグループリーダーだと fork してから新セッションを作り、親は先に
# 終了する。その PID を保存すると、次回の起動でグループを特定できなくなる。
process_group_of() {
  local pid="$1"
  local pgid

  # 相手が既に終了していると `ps` は 1 を返す。`pipefail` と `set -e` の下では
  # そのまま呼び出し側を落としてしまうため、ここで握って空文字として扱う。
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "$pgid" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s' "$pgid"
    return 0
  fi
  printf '%s' "$pid"
}

# 指定 PID のコマンドラインを空白区切りで返す。読めなければ空を返す。
process_command_line() {
  local pid="$1"
  local cmdline="/proc/${pid}/cmdline"

  [[ -r "$cmdline" ]] || return 0
  tr '\0' ' ' < "$cmdline"
}

# コマンドラインが指定した語をすべて含むかを判定する。
#
# 部分一致を語ごとに重ねる。ポート番号を語に含めて呼ぶことで、別ポートで動いている
# 同じアプリケーション (別 worktree の起動など) を巻き込まない。
command_line_matches() {
  local line="$1"
  shift

  local token
  for token in "$@"; do
    [[ "$line" == *"$token"* ]] || return 1
  done
  return 0
}

# 指定 PID の親 PID を返す。読めなければ空。
parent_pid_of() {
  local pid="$1"

  # /proc/<pid>/stat の 4 番目のフィールドが親 PID。実行ファイル名に空白や括弧が
  # 入りうるため、最後の閉じ括弧より後ろを切り出してから数える。
  sed -n 's/^.*) //p' "/proc/${pid}/stat" 2>/dev/null | cut -d' ' -f2
}

# 指定 PID が自分自身・自分の祖先・自分の子孫のいずれかかを判定する。
#
# 検索語は呼び出し側のコマンドラインにそのまま現れる。さらに `< <(...)` のプロセス置換で
# 作られるサブシェルは、親と同じコマンドラインを持つ別 PID として `ps` に現れる。
# PID の直接比較だけでは除ききれないため、双方向に親を辿って判定する。祖先を止めれば
# 自分自身が巻き添えで死ぬため、子孫だけでなく祖先も必ず除く。
is_self_related() {
  local target="$1"
  local cursor depth

  # 自分の祖先か（自分から上へ辿る）。
  cursor="$$"
  depth=0
  while [[ -n "$cursor" && "$cursor" != "0" ]]; do
    [[ "$cursor" == "$target" ]] && return 0
    ((depth += 1))
    ((depth > 64)) && break
    cursor="$(parent_pid_of "$cursor")"
  done

  # 自分の子孫か（相手から上へ辿る）。
  cursor="$target"
  depth=0
  while [[ -n "$cursor" && "$cursor" != "0" ]]; do
    [[ "$cursor" == "$$" ]] && return 0
    ((depth += 1))
    ((depth > 64)) && break
    cursor="$(parent_pid_of "$cursor")"
  done

  return 1
}

# パターンに一致する、自分と同じユーザーのプロセス ID を列挙する。
#
# 自分自身・祖先・子孫は除く。除かないと、検索している自分を止める判定になる。
# `ps` を読んだ時点から実際に停止するまでの間に終了したプロセスを掴まないよう、
# `/proc` から読み直したコマンドラインでも一致を確かめる。既に終了していれば
# 読み出しが空になり、一致しないため候補から外れる。
find_matching_pids() {
  local pid args line
  local -a tokens=("$@")

  while read -r pid args; do
    # 先にパターンで絞る。`is_self_related` は 1 件ごとに `/proc` を辿るため、
    # 全プロセスへ掛けると起動のたびに無視できない待ち時間になる。
    command_line_matches "$args" "${tokens[@]}" || continue
    line="$(process_command_line "$pid")"
    [[ -n "$line" ]] || continue
    command_line_matches "$line" "${tokens[@]}" || continue
    is_self_related "$pid" && continue
    printf '%s\n' "$pid"
  done < <(ps -u "$(id -u)" -o pid=,args= 2>/dev/null)
}

# プロセスグループを停止する。SIGTERM を送り、猶予を過ぎても残っていれば SIGKILL。
#
# グループが既に居ない場合も成功として扱う。止める対象が無いことは異常ではない。
stop_process_group() {
  local pgid="$1"
  local grace="${2:-$APP_PROCESS_STOP_GRACE_SECONDS}"

  kill -TERM -- "-${pgid}" 2>/dev/null || return 0

  # 0.1 秒刻みで様子を見る。1 秒刻みだと、すぐ終わる相手でも猶予いっぱい待つことになり、
  # 起動のたびに体感できる待ち時間が乗る。
  local -r interval_ms=100
  local -r steps=$((grace * 1000 / interval_ms))
  local step=0
  while ((step < steps)); do
    kill -0 -- "-${pgid}" 2>/dev/null || return 0
    sleep 0.1
    ((step += 1))
  done

  kill -KILL -- "-${pgid}" 2>/dev/null || true
  return 0
}

# 前回の起動が残っていれば停止する。
#
#   stop_previous_instance <PIDファイル> <表示名> <一致させる語>...
#
# PID ファイルの指すグループを先に見て、コマンドラインが一致したときだけ止める。
# 一致しなければ PID ファイルだけ片付ける (PID が再利用された、または既に終了した)。
# PID ファイルで止められなかった場合は、パターンに一致するプロセスを探して止める。
# PID ファイルを消したまま実行を続けた場合や、ファイルを書く前に落ちた場合に、
# ポートを掴んだままのプロセスが残らないようにするため。
stop_previous_instance() {
  local pid_file="$1"
  local label="$2"
  shift 2
  local -a tokens=("$@")

  local stopped="false"
  local pgid
  pgid="$(read_pid_file "$pid_file")"

  if [[ -n "$pgid" ]]; then
    local line
    line="$(process_command_line "$pgid")"
    if [[ -n "$line" ]] && command_line_matches "$line" "${tokens[@]}"; then
      printf '[run] 前回の%s (PID %s) を停止します\n' "$label" "$pgid" >&2
      stop_process_group "$pgid"
      stopped="true"
    fi
    rm -f "$pid_file"
  fi

  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    printf '[run] 残っていた%s (PID %s) を停止します\n' "$label" "$pid" >&2
    # 起動時に独立したプロセスグループを作っているため、PID がそのままグループIDになる。
    # グループが無い相手 (手で起動したものなど) には PID 単体でシグナルを送る。
    if kill -0 -- "-${pid}" 2>/dev/null; then
      stop_process_group "$pid"
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
    stopped="true"
  done < <(find_matching_pids "${tokens[@]}")

  [[ "$stopped" == "true" ]] || return 0
  return 0
}
