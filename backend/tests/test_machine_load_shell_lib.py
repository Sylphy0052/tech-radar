"""`scripts/ai-harness/lib/machine-load.sh` の関数のテスト（Issue #84）。

このライブラリは `check.sh` が「今この機械は混んでいるか」を判断するために使う。
Issue #84 のきっかけは、load average 82 / swap 96% の状態で `check.sh` が46分52秒
かかり4ジョブが落ちたことで、そのとき出力からは「壊れたから落ちた」のか「重かったから
落ちた」のかが区別できなかった。

判定そのものが落ちると本末転倒なので、次の2つを固定する。

- `/proc` を読めない環境（`/proc` を持たないOS、権限、想定外の書式）でも `set -euo
  pipefail` の下で落ちないこと。読めないときは「不明」を返して呼び出し側へ委ねる
- 閾値の境界で判定が反転すること。閾値は実測値（下記）を挟む位置に置いてある

読み取り元は `MACHINE_LOAD_PROC_DIR`、コア数は `MACHINE_LOAD_CORES` で差し替える。
実機の `/proc` を読むと結果が機械の状態で変わり、テストが状態を測るものになってしまう。

Issue #84 で実測した「落ちたときの値」（16コア機）:

    load average: 13.97, 55.30, 82.85
    Mem:  13Gi total / 4.9Gi available
    Swap: 8.0Gi total / 7.7Gi used （96%）

1分平均だけを見ると 13.97 / 16 = 87% で、この時点では既に引けている。5分平均が
345% と高く、swap が埋まっている。`MemAvailable` は 4.9Gi 残っており、メモリ量
そのものは指標にならなかった。そのため判定は load average（1分と5分）と swap の
2軸で行い、`MemAvailable` は表示だけに使う。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "ai-harness" / "lib" / "machine-load.sh"

_BASH = shutil.which("bash") or "/bin/bash"
_SHELL_ARGV0 = "./test-entrypoint"

# このライブラリは `log` / `fail` を定義せず呼び出し元のものを使う。テストでも同じ
# 前提で source の前に定義する。
_PREAMBLE = """
log() { printf '%s\\n' "$*" >&2; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
"""

# 実行中のシェルから引き継ぐと結果が変わる変数。テスト側で明示したものだけを渡す。
_DROPPED_FROM_ENV = frozenset(
    {
        "MACHINE_LOAD_PROC_DIR",
        "MACHINE_LOAD_CORES",
        "MACHINE_LOAD_PER_CORE_LIMIT_PERCENT",
    }
)

# Issue #84 で実際に落ちたときの値。閾値はこの値を「混んでいる」と判定する位置に置く。
_CONGESTED_LOADAVG = "13.97 55.30 82.85 3/1234 56789\n"
_CONGESTED_MEMINFO = """MemTotal:       13631488 kB
MemFree:         2936012 kB
MemAvailable:    5138432 kB
SwapTotal:       8388604 kB
SwapFree:         271876 kB
"""

# `machine_is_congested` の終了コードを取り出す断片。`set -e` の下で素のまま呼ぶと
# 非ゼロの時点でシェルが終わり、その後の `printf` へ到達しない。
_CAPTURE_RC = "rc=0; machine_is_congested || rc=$?; printf 'rc=%s' \"$rc\""

# 空いている状態。同じ16コア機で、他に何も動いていないときを想定する。
_IDLE_LOADAVG = "0.52 0.81 1.03 1/900 4242\n"
_IDLE_MEMINFO = """MemTotal:       13631488 kB
MemFree:        10000000 kB
MemAvailable:   11000000 kB
SwapTotal:       8388604 kB
SwapFree:        8388604 kB
"""


def _write_proc(root: Path, loadavg: str, meminfo: str) -> Path:
    """偽の `/proc` を作る。書式ごと差し替えたいので中身は文字列で受ける。"""
    proc = root / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "loadavg").write_text(loadavg, encoding="utf-8")
    (proc / "meminfo").write_text(meminfo, encoding="utf-8")
    return proc


def _run_lib(
    snippet: str,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """ライブラリを読み込んだ `set -euo pipefail` のシェルで `snippet` を実行する。

    入力値は `snippet` へ埋め込まず位置引数として渡す。埋め込むと、テストしたい値
    （`/proc` のパス）がそのままコマンドとして解釈されうる。
    """
    script = f"{_PREAMBLE}\nsource {shlex.quote(str(_LIB))}\n{snippet}\n"
    run_env = {k: v for k, v in os.environ.items() if k not in _DROPPED_FROM_ENV}
    run_env.update(env or {})
    return subprocess.run(  # noqa: S603
        [_BASH, "-euo", "pipefail", "-c", script, _SHELL_ARGV0, *args],
        capture_output=True,
        text=True,
        env=run_env,
        check=False,
        timeout=30,
    )


def _env(proc: Path, cores: str = "16", **overrides: str) -> dict[str, str]:
    base = {"MACHINE_LOAD_PROC_DIR": str(proc), "MACHINE_LOAD_CORES": cores}
    base.update(overrides)
    return base


class TestLoadAverage:
    """`load_average_percent` は load average を百分率の整数で返す。

    bash に浮動小数の演算が無いため、`13.97` を `1397` として扱う。呼び出し側は
    コア数で割って「1コアあたり何パーセントか」を出す。
    """

    def test_1分平均を百分率で返す(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("load_average_percent 1", env=_env(proc))
        assert result.returncode == 0
        assert result.stdout == "1397"

    def test_5分平均を百分率で返す(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("load_average_percent 5", env=_env(proc))
        assert result.returncode == 0
        assert result.stdout == "5530"

    def test_15分平均を百分率で返す(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("load_average_percent 15", env=_env(proc))
        assert result.returncode == 0
        assert result.stdout == "8285"

    def test_小数部が0埋めでも桁を取り違えない(self, tmp_path: Path) -> None:
        """`0.07` は 7 であって 70 ではない。`${v#*.}` の結果をそのまま足すと壊れる。"""
        proc = _write_proc(tmp_path, "0.07 1.00 2.50 1/1 1\n", _IDLE_MEMINFO)
        assert _run_lib("load_average_percent 1", env=_env(proc)).stdout == "7"
        assert _run_lib("load_average_percent 5", env=_env(proc)).stdout == "100"
        assert _run_lib("load_average_percent 15", env=_env(proc)).stdout == "250"

    def test_先頭ゼロを8進数として解釈しない(self, tmp_path: Path) -> None:
        """`08` や `09` を `$(( ))` へ素で渡すと `value too great for base` で落ちる。"""
        proc = _write_proc(tmp_path, "0.08 0.09 8.09 1/1 1\n", _IDLE_MEMINFO)
        result = _run_lib("load_average_percent 1", env=_env(proc))
        assert result.returncode == 0, result.stderr
        assert result.stdout == "8"
        assert _run_lib("load_average_percent 15", env=_env(proc)).stdout == "809"

    def test_ファイルが無くても落ちず不明を返す(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-proc"
        result = _run_lib("load_average_percent 1", env=_env(missing))
        assert result.returncode == 1
        assert result.stdout == ""

    def test_書式が想定と違っても落ちない(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, "not a load average\n", _IDLE_MEMINFO)
        result = _run_lib("load_average_percent 1", env=_env(proc))
        assert result.returncode == 1
        assert result.stdout == ""

    def test_知らない期間を渡すと落ちない(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("load_average_percent 3", env=_env(proc))
        assert result.returncode == 1
        assert result.stdout == ""


class TestSwapUsedPercent:
    def test_使用率を整数で返す(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("swap_used_percent", env=_env(proc))
        assert result.returncode == 0
        # (8388604 - 271876) / 8388604 = 96.7%
        assert result.stdout == "96"

    def test_swapが無い機械では0を返す(self, tmp_path: Path) -> None:
        """`SwapTotal: 0` でゼロ除算しない。swap を切っている環境は珍しくない。"""
        meminfo = _IDLE_MEMINFO.replace(
            "SwapTotal:       8388604 kB", "SwapTotal:             0 kB"
        )
        meminfo = meminfo.replace("SwapFree:        8388604 kB", "SwapFree:              0 kB")
        proc = _write_proc(tmp_path, _IDLE_LOADAVG, meminfo)
        result = _run_lib("swap_used_percent", env=_env(proc))
        assert result.returncode == 0
        assert result.stdout == "0"

    def test_ファイルが無くても落ちず不明を返す(self, tmp_path: Path) -> None:
        result = _run_lib("swap_used_percent", env=_env(tmp_path / "no-proc"))
        assert result.returncode == 1
        assert result.stdout == ""


class TestMemoryAvailableMb:
    def test_MemAvailableをMBで返す(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("memory_available_mb", env=_env(proc))
        assert result.returncode == 0
        # 5138432 kB / 1024 = 5018 MB
        assert result.stdout == "5018"

    def test_ファイルが無くても落ちず不明を返す(self, tmp_path: Path) -> None:
        result = _run_lib("memory_available_mb", env=_env(tmp_path / "no-proc"))
        assert result.returncode == 1
        assert result.stdout == ""


class TestMachineIsCongested:
    """混雑判定。終了コードで返す（0: 混雑、1: 混雑していない、2: 判定できない）。"""

    def test_実際に落ちたときの値を混雑と判定する(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc))
        assert result.stdout == "rc=0"

    def test_空いている機械では混雑と判定しない(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _IDLE_LOADAVG, _IDLE_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc))
        assert result.stdout == "rc=1"

    def test_1分平均だけが高い場合も混雑と判定する(self, tmp_path: Path) -> None:
        """今まさに重い状態。5分平均が追いつく前でも拾う。"""
        proc = _write_proc(tmp_path, "40.00 1.00 1.00 1/1 1\n", _IDLE_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc))
        assert result.stdout == "rc=0"

    def test_5分平均だけが高い場合も混雑と判定する(self, tmp_path: Path) -> None:
        """Issue #84 で踏んだ形。1分平均は引けているが、直前まで重かった。"""
        proc = _write_proc(tmp_path, "1.00 40.00 1.00 1/1 1\n", _IDLE_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc))
        assert result.stdout == "rc=0"

    def test_swapが埋まっていてもloadが低ければ混雑と判定しない(self, tmp_path: Path) -> None:
        """swap は一度埋まると、使われなくなっても解放されない。

        実測で、負荷が引いて 1コアあたり 38% まで下がった後も swap は 96% のまま
        だった。これを混雑の条件に入れると、その機械では毎回警告が出て意味を失う。
        """
        proc = _write_proc(tmp_path, _IDLE_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc))
        assert result.stdout == "rc=1"

    def test_コア数が多ければ同じloadでも混雑と判定しない(self, tmp_path: Path) -> None:
        """判定は1コアあたりで見る。64コアなら load 40 は混んでいない。"""
        proc = _write_proc(tmp_path, "40.00 40.00 40.00 1/1 1\n", _IDLE_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc, cores="64"))
        assert result.stdout == "rc=1"

    def test_閾値は環境変数で上書きできる(self, tmp_path: Path) -> None:
        """閾値は1台の実測から決めた値なので、外れる機械では動かせるようにする。"""
        proc = _write_proc(tmp_path, _IDLE_LOADAVG, _IDLE_MEMINFO)
        result = _run_lib(
            _CAPTURE_RC,
            env=_env(proc, MACHINE_LOAD_PER_CORE_LIMIT_PERCENT="1"),
        )
        assert result.stdout == "rc=0"

    @pytest.mark.parametrize(
        "cores",
        ["notanumber", "16.0", "-4", "two hundred"],
    )
    def test_コア数が数値でなければ判定できないを返す(self, tmp_path: Path, cores: str) -> None:
        """算術式は数値でない文字列を変数名として再展開するため、素通しすると
        `set -u` の下で `unbound variable` になり check.sh ごと落ちる。"""
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc, cores=cores))
        assert result.returncode == 0, result.stderr
        assert result.stdout == "rc=2"

    @pytest.mark.parametrize(
        "limit",
        ["twohundred", "200.0", "-1"],
    )
    def test_閾値が数値でなければ判定できないを返す(self, tmp_path: Path, limit: str) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(
            _CAPTURE_RC,
            env=_env(proc, MACHINE_LOAD_PER_CORE_LIMIT_PERCENT=limit),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "rc=2"

    def test_閾値の先頭ゼロを8進数として解釈しない(self, tmp_path: Path) -> None:
        """`089` を8進数として読むと `value too great for base` で算術式が落ち、
        黙って「混雑していない」へ倒れる。閾値89%なら実測値は混雑側になる。"""
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(
            _CAPTURE_RC,
            env=_env(proc, MACHINE_LOAD_PER_CORE_LIMIT_PERCENT="089"),
        )
        assert result.stderr == "", result.stderr
        assert result.stdout == "rc=0"

    def test_コア数の先頭ゼロを8進数として解釈しない(self, tmp_path: Path) -> None:
        """`08` が 8 として読まれること（8進数エラーで落ちないこと）を固定する。
        実測値の5分平均 5530% を8コアで割ると 691% で、既定の 200% を超える。"""
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc, cores="08"))
        assert result.stderr == "", result.stderr
        assert result.stdout == "rc=0"

    @pytest.mark.parametrize(
        "overrides",
        [{"cores": ""}, {"MACHINE_LOAD_PER_CORE_LIMIT_PERCENT": ""}],
    )
    def test_空文字の上書きは既定値へ倒れる(
        self, tmp_path: Path, overrides: dict[str, str]
    ) -> None:
        """`${VAR:-既定}` は空文字も既定値へ置き換える。判定は実機のコア数に依存する
        ため、rc そのものではなく「判定できない側へ落ちない」ことだけを固定する。"""
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib(_CAPTURE_RC, env=_env(proc, **overrides))
        assert result.stderr == "", result.stderr
        assert result.stdout in ("rc=0", "rc=1")

    def test_procを読めないときは判定できないを返す(self, tmp_path: Path) -> None:
        result = _run_lib(_CAPTURE_RC, env=_env(tmp_path / "no-proc"))
        assert result.stdout == "rc=2"

    def test_判定できなくても呼び出し側を落とさない(self, tmp_path: Path) -> None:
        """`set -e` の下で素の呼び出しが即死しないこと。診断のためのコードで本体を
        止めるのは本末転倒なので、`if` などで囲まずに呼べる必要がある。"""
        result = _run_lib(
            "machine_is_congested || true\nprintf 'reached'",
            env=_env(tmp_path / "no-proc"),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "reached"


class TestDescribeMachineState:
    def test_1行に読める形でまとめる(self, tmp_path: Path) -> None:
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("describe_machine_state", env=_env(proc))
        assert result.returncode == 0
        assert "\n" not in result.stdout
        assert "13.97" in result.stdout
        assert "55.30" in result.stdout
        assert "16" in result.stdout
        assert "96" in result.stdout
        assert "5018" in result.stdout

    def test_読めない項目は不明と書く(self, tmp_path: Path) -> None:
        """全部読めないときに空行を返すと、失敗報告に何も出ずに混乱する。"""
        result = _run_lib("describe_machine_state", env=_env(tmp_path / "no-proc"))
        assert result.returncode == 0
        assert "不明" in result.stdout

    @pytest.mark.parametrize("cores", ["1", "16", "64"])
    def test_コア数を併記する(self, tmp_path: Path, cores: str) -> None:
        """load average は絶対値だけでは重さが分からない。"""
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("describe_machine_state", env=_env(proc, cores=cores))
        assert f"({cores}コア)" in result.stdout

    @pytest.mark.parametrize("cores", ["auto", "16.0", "not a number"])
    def test_コア数が数値でなければ不明と書く(self, tmp_path: Path, cores: str) -> None:
        """生の値をそのまま出すと `(autoコア)` のような嘘の表示になる。"""
        proc = _write_proc(tmp_path, _CONGESTED_LOADAVG, _CONGESTED_MEMINFO)
        result = _run_lib("describe_machine_state", env=_env(proc, cores=cores))
        assert result.returncode == 0, result.stderr
        assert "(不明コア)" in result.stdout
        assert cores not in result.stdout
