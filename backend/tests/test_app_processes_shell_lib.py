"""`scripts/ai-harness/lib/app_processes.sh` の関数のテスト。

`run.sh` は起動のたびに、前回の backend / frontend が残っていれば先に止める。
止める対象を間違えると無関係のプロセスを殺してしまうため、判定は次の2段構えにする。

1. 前回の実行が書いた PID ファイル（プロセスグループID）を読む
2. その ID のリーダープロセスのコマンドラインが、期待するパターンに一致するかを確かめる

PID は再利用されるため、1 だけでは足りない。異常終了で PID ファイルが残り、その番号を
別のプロセスが使っていれば、無関係なものを止めてしまう。逆に 2 だけでも足りない。
`next dev` は `next-server` という別名の子を持ち、その子のコマンドラインにはポート番号が
入らないため、パターン一致では取りこぼす。

シェル関数は `bash -euo pipefail -c` の子プロセスとして呼び、標準出力と終了コードを
固定する。`postgres.sh` のテスト（`test_postgres_shell_lib.py`）と同じ枠組み。

実際にプロセスを止める関数は、`sleep` を起動してその PID / プロセスグループを渡すことで
確かめる。テストが自分自身や無関係のプロセスを止めないよう、対象はすべてテスト内で
起動したものに限る。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "ai-harness" / "lib" / "app_processes.sh"
_BASH = "/bin/bash"
# 部分パスでの実行を避けるため、解決済みの絶対パスを使う。
_PS = shutil.which("ps") or "/bin/ps"


def _run(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """ライブラリを読み込んだ上で `script` を実行する。"""
    command = f"source {_LIB}\n{script}"
    merged_env = dict(os.environ)
    if env is not None:
        merged_env.update(env)
    return subprocess.run(  # noqa: S603
        [_BASH, "-euo", "pipefail", "-c", command],
        capture_output=True,
        text=True,
        env=merged_env,
        check=False,
    )


_spawned: list[subprocess.Popen[bytes]] = []


def _spawn_group(command: str) -> int:
    """新しいプロセスグループでコマンドを起動し、そのグループIDを返す。

    コマンドに目印を埋める場合、単一コマンドだけを渡してはいけない。bash は最後の
    1コマンドを `exec` で自分自身に置き換えるため、コマンドラインから目印が消える。
    テスト側では `sleep 30 && true # 目印` のように複合コマンドにして残す。
    """
    process = subprocess.Popen(  # noqa: S603
        [_BASH, "-c", command],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _spawned.append(process)
    return process.pid


def _group_is_alive(pgid: int) -> bool:
    """プロセスグループに、まだ動いているプロセスが残っているか。

    `os.killpg(pgid, 0)` では判定できない。停止済みでも、親がまだ回収していない
    ゾンビが1つでも残っていればシグナルは通り、生存と誤って判定してしまう。
    `ps` の状態列を読み、ゾンビ (`Z`) を除いて数える。
    """
    result = subprocess.run(  # noqa: S603
        [_PS, "-eo", "pgid=,stat="],
        capture_output=True,
        text=True,
        check=False,
    )
    for row in result.stdout.splitlines():
        fields = row.split()
        if len(fields) < 2:
            continue
        if fields[0] == str(pgid) and not fields[1].startswith("Z"):
            return True
    return False


def _wait_until_gone(pgid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # 終了済みの子を回収し、ゾンビを残さない。
        for process in _spawned:
            process.poll()
        if not _group_is_alive(pgid):
            return True
        time.sleep(0.05)
    return False


# ---- read_pid_file ----------------------------------------------------------


def test_read_pid_file_returns_value(tmp_path: Path) -> None:
    """正の整数だけが書かれていればそのまま返す。"""
    pid_file = tmp_path / "backend.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    result = _run(f'read_pid_file "{pid_file}"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == "12345"


def test_read_pid_file_returns_empty_when_missing(tmp_path: Path) -> None:
    """ファイルが無くても失敗しない（`set -e` の下でも落ちない）。"""
    result = _run(f'read_pid_file "{tmp_path / "absent.pid"}"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("content", ["", "  ", "abc", "-1", "0", "12 34", "1e3"])
def test_read_pid_file_rejects_invalid_content(tmp_path: Path, content: str) -> None:
    """PID として解釈できない値は空として扱う。誤った番号へシグナルを送らないため。"""
    pid_file = tmp_path / "broken.pid"
    pid_file.write_text(content, encoding="utf-8")

    result = _run(f'read_pid_file "{pid_file}"')

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# ---- process_command_line ---------------------------------------------------


def test_process_command_line_reads_own_cmdline() -> None:
    """自分自身のコマンドラインを空白区切りで返す。"""
    result = _run('process_command_line "$$"')

    assert result.returncode == 0, result.stderr
    assert "bash" in result.stdout


def test_process_command_line_is_empty_for_unknown_pid() -> None:
    """存在しない PID でも失敗せず空を返す。"""
    # PID の最大値より大きい値を使う。実在しないことが確実。
    result = _run("process_command_line 4194305")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


# ---- process_group_of -------------------------------------------------------


def test_process_group_of_returns_group_id() -> None:
    """新しいセッションで起動したプロセスは、自分自身がグループIDになる。"""
    pgid = _spawn_group("sleep 30 && true # techradar-test-marker-pgid")
    try:
        result = _run(f"process_group_of {pgid}")

        assert result.returncode == 0, result.stderr
        assert result.stdout == str(pgid)
    finally:
        os.killpg(pgid, signal.SIGKILL)


def test_process_group_of_falls_back_to_pid() -> None:
    """既に終了した PID では取得できないため、渡された PID をそのまま返す。

    保存する値が空になると、次回の起動で前回のプロセスを特定する手がかりを失う。
    グループを引けなくても、PID を残しておけば判定の材料になる。
    """
    result = _run("process_group_of 4194305")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "4194305"


# ---- command_line_matches ---------------------------------------------------


def test_command_line_matches_requires_all_tokens() -> None:
    """指定した語をすべて含むときだけ一致とみなす。"""
    line = "uv run uvicorn techradar.main:app --reload --host 127.0.0.1 --port 18700"

    matched = _run(f"command_line_matches '{line}' 'uvicorn techradar.main:app' '--port 18700'")
    assert matched.returncode == 0, matched.stderr


def test_command_line_matches_rejects_other_port() -> None:
    """ポート番号が違えば別インスタンスとして扱い、一致させない。"""
    line = "uv run uvicorn techradar.main:app --reload --host 127.0.0.1 --port 18800"

    result = _run(f"command_line_matches '{line}' 'uvicorn techradar.main:app' '--port 18700'")

    assert result.returncode == 1


def test_command_line_matches_rejects_other_application() -> None:
    """同じポートでも別アプリなら一致させない。"""
    line = "uvicorn other.app:app --port 18700"

    result = _run(f"command_line_matches '{line}' 'uvicorn techradar.main:app' '--port 18700'")

    assert result.returncode == 1


# ---- find_matching_pids -----------------------------------------------------


def test_find_matching_pids_finds_running_process() -> None:
    """パターンに一致する自分のプロセスを見つける。"""
    marker = "techradar-test-marker-find"
    pgid = _spawn_group(f"sleep 30 && true # {marker}")
    try:
        result = _run(f"find_matching_pids 'sleep 30' '{marker}'")

        assert result.returncode == 0, result.stderr
        assert str(pgid) in result.stdout.split()
    finally:
        os.killpg(pgid, signal.SIGKILL)


def test_find_matching_pids_returns_empty_when_absent() -> None:
    """一致するものが無ければ空を返し、失敗もしない。"""
    result = _run("find_matching_pids 'techradar-nonexistent-pattern-zzz'")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_find_matching_pids_excludes_self() -> None:
    """自分自身のプロセスは候補に含めない。検索語がそのまま自分の引数に現れるため。"""
    result = _run("find_matching_pids 'find_matching_pids'")

    assert result.returncode == 0, result.stderr
    own_pids = result.stdout.split()
    assert str(os.getpid()) not in own_pids


# ---- stop_process_group -----------------------------------------------------


def test_stop_process_group_terminates_group() -> None:
    """プロセスグループごと停止する。子孫が残らないことを確かめる。"""
    pgid = _spawn_group("sleep 30 & sleep 30 & wait")
    try:
        result = _run(f"stop_process_group {pgid} 1")

        assert result.returncode == 0, result.stderr
        assert _wait_until_gone(pgid), "プロセスグループが残っている"
    finally:
        if _group_is_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_stop_process_group_kills_when_term_is_ignored() -> None:
    """SIGTERM を無視するプロセスは、猶予を過ぎたら SIGKILL で止める。"""
    pgid = _spawn_group("trap '' TERM; sleep 30")
    try:
        result = _run(f"stop_process_group {pgid} 1")

        assert result.returncode == 0, result.stderr
        assert _wait_until_gone(pgid), "SIGKILL でも止まっていない"
    finally:
        if _group_is_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_stop_process_group_ignores_unknown_group() -> None:
    """既に居ないグループを指定しても失敗しない。"""
    result = _run("stop_process_group 4194305 1")

    assert result.returncode == 0, result.stderr


# ---- stop_previous_instance -------------------------------------------------


def test_stop_previous_instance_stops_process_from_pid_file(tmp_path: Path) -> None:
    """PID ファイルの指すグループが期待どおりのコマンドなら停止する。"""
    marker = "techradar-test-marker-pidfile"
    pgid = _spawn_group(f"sleep 30 && true # {marker}")
    pid_file = tmp_path / "app.pid"
    pid_file.write_text(f"{pgid}\n", encoding="utf-8")
    try:
        result = _run(f"stop_previous_instance '{pid_file}' 'テスト対象' 'sleep 30' '{marker}'")

        assert result.returncode == 0, result.stderr
        assert _wait_until_gone(pgid), "停止できていない"
        assert not pid_file.exists(), "停止後に PID ファイルが残っている"
        assert "テスト対象" in result.stderr
    finally:
        if _group_is_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_stop_previous_instance_keeps_unrelated_process(tmp_path: Path) -> None:
    """PID が再利用されていた場合、コマンドラインが一致しないので止めない。"""
    pgid = _spawn_group("sleep 30 && true # techradar-test-marker-unrelated")
    pid_file = tmp_path / "app.pid"
    pid_file.write_text(f"{pgid}\n", encoding="utf-8")
    try:
        result = _run(
            f"stop_previous_instance '{pid_file}' 'テスト対象' 'uvicorn techradar.main:app'"
        )

        assert result.returncode == 0, result.stderr
        assert _group_is_alive(pgid), "無関係のプロセスを停止している"
        # 一致しなかった PID ファイルは、次回も同じ判定を繰り返さないよう片付ける。
        assert not pid_file.exists()
    finally:
        os.killpg(pgid, signal.SIGKILL)


def test_stop_previous_instance_stops_process_without_pid_file(tmp_path: Path) -> None:
    """PID ファイルが無くても、パターンに一致するプロセスがあれば停止する。

    PID ファイルを消したまま実行を続けた場合や、ファイルを書く前に落ちた場合に、
    ポートを掴んだままのプロセスが残らないようにするため。
    """
    marker = "techradar-test-marker-fallback"
    pgid = _spawn_group(f"sleep 30 && true # {marker}")
    try:
        result = _run(
            f"stop_previous_instance '{tmp_path / 'absent.pid'}' 'テスト対象' 'sleep 30' '{marker}'"
        )

        assert result.returncode == 0, result.stderr
        assert _wait_until_gone(pgid), "停止できていない"
    finally:
        if _group_is_alive(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_stop_previous_instance_is_quiet_when_nothing_runs(tmp_path: Path) -> None:
    """止める対象が無ければ何も出さずに終わる。"""
    result = _run(
        f"stop_previous_instance '{tmp_path / 'absent.pid'}' 'テスト対象' "
        "'techradar-nonexistent-pattern-zzz'"
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


# ---- write_pid_file ---------------------------------------------------------


def test_write_pid_file_creates_parent_directory(tmp_path: Path) -> None:
    """置き場所のディレクトリが無ければ作る。"""
    pid_file = tmp_path / "nested" / "app.pid"

    result = _run(f'write_pid_file "{pid_file}" 4321')

    assert result.returncode == 0, result.stderr
    assert pid_file.read_text(encoding="utf-8").strip() == "4321"


# ---- remove_pid_file_if_matches ---------------------------------------------


def test_remove_pid_file_if_matches_removes_own_file(tmp_path: Path) -> None:
    """自分が書いた値と一致すれば消す。"""
    pid_file = tmp_path / "app.pid"
    pid_file.write_text("4321\n", encoding="utf-8")

    result = _run(f'remove_pid_file_if_matches "{pid_file}" 4321')

    assert result.returncode == 0, result.stderr
    assert not pid_file.exists()


def test_remove_pid_file_if_matches_keeps_other_file(tmp_path: Path) -> None:
    """別の実行が書き換えた後なら消さない。

    新しい実行が古い実行を止めると、古い側の後片付けは相手が値を書き終えた後に走る。
    無条件に消すと、動いているインスタンスの PID ファイルを失う。
    """
    pid_file = tmp_path / "app.pid"
    pid_file.write_text("9999\n", encoding="utf-8")

    result = _run(f'remove_pid_file_if_matches "{pid_file}" 4321')

    assert result.returncode == 0, result.stderr
    assert pid_file.read_text(encoding="utf-8").strip() == "9999"


def test_remove_pid_file_if_matches_tolerates_missing_file(tmp_path: Path) -> None:
    """ファイルが無くても失敗しない。"""
    result = _run(f'remove_pid_file_if_matches "{tmp_path / "absent.pid"}" 4321')

    assert result.returncode == 0, result.stderr
