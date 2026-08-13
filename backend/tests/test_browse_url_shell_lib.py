"""`scripts/ai-harness/lib/browse_url.sh` の関数のテスト（Issue #85）。

`run.sh` は起動時に「どこを開けばよいか」を案内する。この案内が `BIND_HOST` を
そのまま流用していたため、既定の `127.0.0.1` で案内され、そのとおりに開くと
API 呼び出しがすべて CORS で弾かれていた（許可オリジンは `http://localhost:13700`
であり、`localhost` と `127.0.0.1` は CORS 上は別オリジンになる）。

`BIND_HOST` は listen するインターフェースの指定であって、ブラウザで開く URL の
ホストとは別物である。特に `0.0.0.0` は「全インターフェース」を表す特殊なアドレスで、
そもそも開く先にならない。この違いをここで固定する。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "ai-harness" / "lib" / "browse_url.sh"

_BASH = shutil.which("bash") or "/bin/bash"
_SHELL_ARGV0 = "./test-entrypoint"

# このライブラリは `log` / `fail` を定義せず呼び出し元のものを使う。テストでも同じ
# 前提で source の前に定義する。
_PREAMBLE = """
log() { printf '%s\\n' "$*" >&2; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
"""


def _run_lib(snippet: str, *args: str) -> subprocess.CompletedProcess[str]:
    """ライブラリを source した子シェルで `snippet` を実行する。

    可変値は文字列へ埋め込まず `*args` として渡す。埋め込むと、値に引用符が
    含まれたときにスニペットの構造そのものが変わってしまう。
    """
    script = f"{_PREAMBLE}\nsource {shlex.quote(str(_LIB))}\n{snippet}\n"
    return subprocess.run(  # noqa: S603
        [_BASH, "-euo", "pipefail", "-c", script, _SHELL_ARGV0, *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )


class TestBrowseHost:
    @pytest.mark.parametrize(
        "bind_host",
        # S104: ここで bind するわけではなく、案内へ変換する入力として渡すだけである。
        ["127.0.0.1", "localhost", "0.0.0.0", "::", "::1", ""],  # noqa: S104
    )
    def test_ループバックと全インターフェースはlocalhostへ倒す(self, bind_host: str) -> None:
        """CORS の許可オリジンが `http://localhost:<port>` であるため、案内も
        localhost に揃える必要がある。`0.0.0.0` / `::` はそもそも開く先にならない。"""
        result = _run_lib('browse_host "$1"', bind_host)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "localhost"

    @pytest.mark.parametrize("bind_host", ["192.168.1.10", "10.0.0.5", "techradar.local"])
    def test_特定のホストはそのまま案内する(self, bind_host: str) -> None:
        """別端末から開く運用では、その端末から届くホストで案内しないと意味がない
        （`.env.example` のとおり `CORS_ALLOW_ORIGINS` は利用者が揃える）。"""
        result = _run_lib('browse_host "$1"', bind_host)
        assert result.returncode == 0, result.stderr
        assert result.stdout == bind_host

    @pytest.mark.parametrize("bind_host", ["LOCALHOST", "LocalHost", "0.0.0.0 "])
    def test_大文字のループバック表記もlocalhostへ倒す(self, bind_host: str) -> None:
        """ホスト名の解決は大小を区別しないため `LOCALHOST` でも起動できてしまうが、
        CORS の許可オリジンは小文字である。素通しすると案内どおりに開いた先で弾かれる。

        末尾に空白が付いた値は倒さない。`run.sh` は空白のみの `BIND_HOST` を弾くだけで
        トリムはせず、その値のまま listen を試みるため、案内だけ別のホストへ倒すと
        かえって食い違う。
        """
        result = _run_lib('browse_host "$1"', bind_host)
        assert result.returncode == 0, result.stderr
        expected = "localhost" if bind_host.strip() == bind_host else bind_host
        assert result.stdout == expected

    def test_引数が無くてもlocalhostを返す(self) -> None:
        """`set -u` の下で未定義参照にならないこと。案内のための関数が起動を
        止めるのは本末転倒である。"""
        result = _run_lib("browse_host")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "localhost"

    def test_改行を含む値を1行へ潰さない(self) -> None:
        """想定外の値をそのまま返すこと自体は許すが、案内文の行構造を壊さないよう
        値の扱いは printf に委ねる（`echo` だとエスケープ解釈が処理系で変わる）。"""
        result = _run_lib('browse_host "$1"', "example.com\nevil")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "example.com\nevil"
