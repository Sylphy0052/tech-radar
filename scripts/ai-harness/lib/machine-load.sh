#!/usr/bin/env bash
# 機械の混み具合を読む（Issue #84）。
#
# check.sh は手動実行が前提で（Issue #76）、CI も無い（Issue #82）ため、その赤が
# 唯一の信号である。ところが混んだ機械で回すと、変更が壊れていなくてもジョブが落ちる。
# 実測では load average 82 / swap 96% の状態で 46分52秒かかり、pytest・vitest・
# audit 2つが落ちた。負荷が引いた後に同じ木で回すと 2分2秒で全緑だった。
# 出力からこの2つを区別できるようにするために、機械の状態を読む。
#
# 呼び出し元の `log` / `fail` を使う。単体で実行するものではない。
#
# **ここで落ちて本体を止めない。** 診断のためのコードが check.sh を止めるのは本末転倒
# なので、読めない項目は「不明」として返し、判断は呼び出し側へ委ねる。`/proc` を持たない
# OS でも source できる。

# 読み取り元。テストから固定値を食わせるために変数にしてある。
MACHINE_LOAD_PROC_DIR="${MACHINE_LOAD_PROC_DIR:-/proc}"
MACHINE_LOAD_CORES="${MACHINE_LOAD_CORES:-$(nproc 2>/dev/null || echo 1)}"

# 1コアあたりの load average の上限（百分率）。実測（Issue #84）では、落ちたときの
# 5分平均が 345%、空いているときが 5% だった。その間で桁を離して 200% に置く。
# つまり「実行可能なプロセスがコア数の2倍たまっている」状態を混雑と呼ぶ。
MACHINE_LOAD_PER_CORE_LIMIT_PERCENT="${MACHINE_LOAD_PER_CORE_LIMIT_PERCENT:-200}"

# swap 使用率と `MemAvailable` は表示だけに使い、判定には使わない。
#
# swap は一度埋まると使われなくなっても解放されない。実測で、負荷が引いて load が
# 1コアあたり 38% まで下がった後も swap は 96% のままだった。これを混雑の条件に
# 入れると、その機械では毎回警告が出て意味を失う。`MemAvailable` も落ちたときに
# 4.9GB 残っており指標にならなかった。
#
# どちらも「混んでいるときに何が起きていたか」を読むには役立つため、
# `describe_machine_state` には出す。

# "13.97" を 1397 にする。bash に浮動小数の演算が無いため、百分率の整数で持ち回す。
_load_to_percent() {
  local raw="$1" int frac
  [[ "$raw" =~ ^[0-9]+\.[0-9]+$ ]] || return 1
  int="${raw%%.*}"
  frac="${raw#*.}"
  # 小数部の桁数は環境で変わりうる。2桁へ揃えてから足す。`0.07` は 7 であって 70 では
  # ないため、右詰めではなく左から2桁を取る。
  frac="${frac}00"
  frac="${frac:0:2}"
  # `10#` を付ける。付けないと `08` や `09` が8進数として解釈され、`value too great
  # for base` でスクリプトごと落ちる。
  printf '%s' "$((10#$int * 100 + 10#$frac))"
}

# load average を百分率の整数で返す。期間は 1 / 5 / 15 のいずれか。
load_average_percent() {
  local period="${1:-}" one five fifteen rest file
  file="$MACHINE_LOAD_PROC_DIR/loadavg"
  [[ -r "$file" ]] || return 1
  read -r one five fifteen rest <"$file" 2>/dev/null || return 1
  case "$period" in
    1) _load_to_percent "$one" ;;
    5) _load_to_percent "$five" ;;
    15) _load_to_percent "$fifteen" ;;
    *) return 1 ;;
  esac
}

# `/proc/meminfo` から `<key>: <数値> kB` の数値部分を取り出す。
_meminfo_kb() {
  local key="$1" file line value
  file="$MACHINE_LOAD_PROC_DIR/meminfo"
  [[ -r "$file" ]] || return 1
  # grep は該当が無いと 1 を返す。swap を持たないカーネル構成では SwapTotal 自体が無い。
  line="$(grep -m1 "^${key}:" "$file" 2>/dev/null)" || return 1
  value="${line#*:}"
  value="${value%kB}"
  value="${value//[[:space:]]/}"
  [[ "$value" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$value"
}

# swap の使用率を整数の百分率で返す。swap を切っている機械では 0 を返す。
swap_used_percent() {
  local total free
  total="$(_meminfo_kb SwapTotal)" || return 1
  free="$(_meminfo_kb SwapFree)" || return 1
  if ((total <= 0)); then
    printf '0'
    return 0
  fi
  printf '%s' "$(((total - free) * 100 / total))"
}

# 割り当て可能なメモリを MB で返す。
memory_available_mb() {
  local kb
  kb="$(_meminfo_kb MemAvailable)" || return 1
  printf '%s' "$((kb / 1024))"
}

# 混んでいるかを終了コードで返す。0: 混雑、1: 混雑していない、2: 判定できない。
#
# load average は1分と5分の両方を見る。1分だけだと、直前まで重くて今まさに引けている
# 状態（Issue #84 で踏んだ形。ジョブが落ちた後に測ると 1分平均は 87% まで下がっていた）
# を拾えない。5分だけだと、走り始めたばかりの重さを拾えない。
machine_is_congested() {
  local one five
  local cores="$MACHINE_LOAD_CORES"
  local limit="$MACHINE_LOAD_PER_CORE_LIMIT_PERCENT"

  # 算術式は数値でない文字列を変数名として再展開するため、`set -u` の下では
  # `unbound variable` でシェルごと落ちる。上書き用の2変数は人が手で渡すので、
  # 算術式へ入れる前に数値であることを確かめる。`10#` は `08` の8進数解釈を防ぐ
  # （_load_to_percent と同じ理由。こちらは黙って「混雑していない」へ倒れるため
  # 落ちるより厄介である）。
  if [[ ! "$cores" =~ ^[0-9]+$ ]] || [[ ! "$limit" =~ ^[0-9]+$ ]]; then
    return 2
  fi
  cores="10#$cores"
  limit="10#$limit"

  one="$(load_average_percent 1)" || one=""
  five="$(load_average_percent 5)" || five=""

  if [[ -z "$one" || -z "$five" ]] || ((cores <= 0)); then
    return 2
  fi

  # `&&` と `||` で書くと、条件が偽のときに文全体が失敗して `set -e` に殺される。
  if ((one / cores >= limit)); then
    return 0
  fi
  if ((five / cores >= limit)); then
    return 0
  fi
  return 1
}

# 百分率の整数を load average の見た目へ戻す。
_percent_to_load() {
  local p="${1:-}"
  if [[ ! "$p" =~ ^[0-9]+$ ]]; then
    printf '不明'
    return 0
  fi
  printf '%d.%02d' "$((p / 100))" "$((p % 100))"
}

_percent_or_unknown() {
  local v="${1:-}"
  if [[ ! "$v" =~ ^[0-9]+$ ]]; then
    printf '不明'
    return 0
  fi
  printf '%s%%' "$v"
}

# コア数は環境変数で上書きできるため、数値とは限らない。整数でなければ「不明」と
# 書く。生の値をそのまま出すと `(autoコア)` のような嘘の表示になり、ついでに制御
# 文字を含む値が端末へ流れる。
_cores_or_unknown() {
  local v="${1:-}"
  if [[ ! "$v" =~ ^[0-9]+$ ]]; then
    printf '不明'
    return 0
  fi
  printf '%s' "$((10#$v))"
}

_megabytes_or_unknown() {
  local v="${1:-}"
  if [[ ! "$v" =~ ^[0-9]+$ ]]; then
    printf '不明'
    return 0
  fi
  printf '%sMB' "$v"
}

# 機械の状態を1行にまとめる。失敗報告へそのまま添える。
# load average は絶対値だけでは重さが分からないため、コア数を併記する。
describe_machine_state() {
  local one five swap avail
  one="$(load_average_percent 1)" || one=""
  five="$(load_average_percent 5)" || five=""
  swap="$(swap_used_percent)" || swap=""
  avail="$(memory_available_mb)" || avail=""
  printf 'load %s / %s (%sコア), swap %s, 空きメモリ %s' \
    "$(_percent_to_load "$one")" \
    "$(_percent_to_load "$five")" \
    "$(_cores_or_unknown "$MACHINE_LOAD_CORES")" \
    "$(_percent_or_unknown "$swap")" \
    "$(_megabytes_or_unknown "$avail")"
}
