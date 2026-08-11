"""`scripts/ai-harness/lib/postgres.sh` の純粋な関数のテスト（Issue #68）。

このライブラリは commit 前ゲートである `check.sh` と `run.sh` の両方から読み込まれる。
落ちる経路が入ると開発が止まり、判定が緩む経路が入ると公開範囲の警告が静かに効かなく
なるにもかかわらず、Issue #65 で関数群を足した時点では自動テストが無かった。Issue #65 の
self review で実測によって初めて見つかった次の3件は、いずれも入出力を固定していれば
早い段階で分かる種類のものだった。

- 設定ファイルが無いとき `pipefail` と `set -e` でスクリプト全体が落ちる
- 公開先の判定が部分一致で、`127.0.0.1` が `127.0.0.10` に一致して見逃す
- 環境変数だけを見ていたため、設定ファイルで広げた運用に対して毎回誤警告が出る

シェル関数は `bash -euo pipefail -c` の子プロセスとして呼び、標準出力・標準エラー・
終了コードを固定する。呼び出しごとに独立したシェルなので、`set -euo pipefail` の下で
落ちないこと（受入基準）がそのまま終了コードで確かめられる。

docker には触れない。`warn_if_published_host_differs` と `docker_is_reachable` は
`PATH` の先頭へ置いた偽の `docker` で出力と終了コードを差し替える。実dockerを使うと
実行環境によって結果が変わり、テストが環境の状態を測るものになってしまうため。

`assert_docker_usable` と `assert_docker_reachable` の案内文も同じ枠組みで固定する
（Issue #70）。この2つはユーザーがそのまま読んで実行する文言を組み立てるため、
埋め込みが壊れると「案内どおり実行したのに何も起きない」「停止のつもりで起動する」に
なる。未インストールの経路は `PATH` を空のディレクトリだけにして再現する。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "ai-harness" / "lib" / "postgres.sh"

# `PATH` を空にするテストがあるため、シェル自身は絶対パスで起動する。
_BASH = shutil.which("bash") or "/bin/bash"
# `sg` が案内文の中身を実行するときのシェル（man sg より `/bin/sh`）。
_SH = shutil.which("sh") or "/bin/sh"
# 子シェルの `$0`。`ENTRYPOINT_SCRIPT` が無いとき案内文がここへ落ちる。
# 案内へそのまま載せてよい文字だけで組む（`assert_docker_reachable` は非ASCIIを
# 安全側へ倒して一行の案内を出さないため、日本語の名前にすると別の分岐を見てしまう）。
_SHELL_ARGV0 = "./test-entrypoint"

# docker group が現在のシェルへ反映されていないときに docker が返すエラー。
# この文字列を含むかどうかで案内の出し分けが決まる（Issue #55）。
_PERMISSION_DENIED = "permission denied while trying to connect to the Docker daemon socket"

# 全インターフェースへの公開先。ここで待ち受けるわけではなく、テストデータとして
# 「広げた公開先」を表すために使う。他のケースはドキュメント用に予約された
# TEST-NET-1（RFC 5737）を主に使い、判定の境界を見たいところだけプライベート
# アドレス（10.0.0.0/8）を混ぜる。いずれも文字列の比較にしか使わず接続はしない。
_ALL_INTERFACES = "0.0.0.0"  # noqa: S104

# このライブラリは `log` / `fail` を定義せず呼び出し元のものを使う。テストでも同じ前提で
# source の前に定義する。どちらも標準エラーへ出し、標準出力は関数の戻り値だけにする。
_PREAMBLE = """
log() { printf '%s\\n' "$*" >&2; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
"""

# 実行中のシェルから引き継ぐと結果が変わる変数。テスト側で明示したものだけを渡す。
# `./run.sh` を実行したシェルから pytest を叩くと `BIND_HOST` などが export 済みになる。
_DROPPED_FROM_ENV = frozenset(
    {
        "BIND_HOST",
        "ENV_FILE",
        "COMPOSE_FILE",
        "_DOCKER_REACHABLE",
        "FAKE_DOCKER_PS_OUTPUT",
        "FAKE_DOCKER_STDERR",
        "FAKE_DOCKER_EXIT",
        "FAKE_DOCKER_CALLS",
        "FAKE_DOCKER_ENV_DUMP",
        # 実行中のシェルが export していると、ロケールを固定しているのか
        # 引き継いだだけなのかが見分けられなくなる。
        "LC_ALL",
        "ENTRYPOINT_SCRIPT",
        "ENTRYPOINT_ARGS",
    }
)


def _run_lib(
    snippet: str,
    *args: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """ライブラリを読み込んだ `set -euo pipefail` のシェルで `snippet` を実行する。

    入力値は `snippet` へ埋め込まず位置引数として渡す。埋め込むと、テストしたい値
    （設定ファイルのパスやホスト名）がそのままコマンドとして解釈されうる。
    """
    script = f"{_PREAMBLE}\nsource {shlex.quote(str(_LIB))}\n{snippet}\n"
    run_env = {k: v for k, v in os.environ.items() if k not in _DROPPED_FROM_ENV}
    run_env.update(env or {})
    return subprocess.run(  # noqa: S603
        [_BASH, "-euo", "pipefail", "-c", script, _SHELL_ARGV0, *args],
        capture_output=True,
        text=True,
        env=run_env,
        cwd=str(cwd),
        check=False,
        timeout=30,
    )


def _capture(call: str) -> str:
    """関数を素で呼んでから、同じ呼び出しの標準出力をマーカーで囲んで出す。

    値の比較だけならコマンド置換で足りる。ただしコマンド置換はサブシェルなので、
    `set -e` で関数が落ちても呼び出し側には代入の失敗としてしか伝わらず、
    `||` の右辺に置かれた呼び出しではそれも打ち消される。実際、設定ファイル不在で
    落ちる経路（Issue #65）はコマンド置換だけでは再現しない（実装の防御を外しても
    テストが通ってしまうことを実測で確認した）。先に素で呼んで終了コードを表に出す。

    マーカーで囲むのは、前後の空白が残っていないことまで見るため。
    """
    return f'{call} >/dev/null\nprintf "[%s]" "$({call})"'


_HINT_MARKER = "sg docker -c "
_HINT_TAIL = " のように実行してください"


def _hint_text(stderr: str) -> str:
    """案内文のうち `sg docker -c` へ渡している部分を取り出す。

    テストが渡す値にこの区切り文言そのものを含めないこと。含めると途中で切れる。
    """
    assert _HINT_MARKER in stderr, stderr
    tail = stderr.split(_HINT_MARKER, 1)[1]
    return tail.split(_HINT_TAIL, 1)[0]


def _hint_words(stderr: str) -> list[str]:
    """案内の引数部分を、シェルの語へ分解する。

    引用が閉じているかを目視ではなく機械で見るため。閉じていれば語は1つになり、
    閉じていなければ後続が引用の外へ出るぶん語が増える（引用が開いたままなら
    `shlex.split` が `ValueError` を投げる）。
    """
    return shlex.split(_hint_text(stderr))


def _simulate_sg(stderr: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """`sg docker -c <案内の中身>` が実際にやることを模す。

    `sg` は受け取った文字列を `/bin/sh` で実行する（man sg）。案内をコピーして
    実行したとき何が動くかは、案内文を読むシェルが引用を解く段と、`sg` の内側で
    シェルが解釈する段の両方を通してしか分からない。引用が1語に収まっていることを
    見るだけでは、`;` や `$(...)` が内側で効くことを捕まえられない（Issue #71）。

    ここで実行するのは、テストが用意した無害なスクリプトだけに限る。
    """
    return subprocess.run(  # noqa: S603
        [_BASH, "-c", f"{_SH} -c {_hint_text(stderr)}"],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        check=False,
        timeout=10,
    )


def _write_env_file(tmp_path: Path, content: str) -> Path:
    """設定ファイルを作る。`newline=""` で改行をそのまま書き、CRLF を再現する。"""
    path = tmp_path / "env-file"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


@pytest.fixture
def fake_docker_bin(tmp_path: Path) -> Path:
    """`PATH` の先頭へ置く偽の `docker` を作り、そのディレクトリを返す。

    引数は見ずに `FAKE_DOCKER_PS_OUTPUT` を標準出力へ、`FAKE_DOCKER_STDERR` を標準
    エラーへ出して `FAKE_DOCKER_EXIT` で終わる。標準エラーを分けているのは
    `assert_docker_reachable` が `docker info 2>&1 >/dev/null` で標準エラーだけを
    拾うため。`FAKE_DOCKER_CALLS` が指定されていれば呼び出しを1行ずつ追記する
    （`docker_is_reachable` のメモ化を回数で確かめるため）。
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ -n "${FAKE_DOCKER_CALLS:-}" ]]; then\n'
        '  printf "%s\\n" "$*" >>"$FAKE_DOCKER_CALLS"\n'
        "fi\n"
        'if [[ -n "${FAKE_DOCKER_ENV_DUMP:-}" ]]; then\n'
        '  printf "LC_ALL=%s\\n" "${LC_ALL:-unset}" >>"$FAKE_DOCKER_ENV_DUMP"\n'
        "fi\n"
        'printf "%s" "${FAKE_DOCKER_PS_OUTPUT:-}"\n'
        'printf "%s" "${FAKE_DOCKER_STDERR:-}" >&2\n'
        'exit "${FAKE_DOCKER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    # 実行するのはテスト自身だけなので、所有者以外へは開けない。
    script.chmod(0o700)
    return bin_dir


def _path_with(bin_dir: Path) -> str:
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


class TestTrimSpaces:
    """`trim_spaces`: 前後の空白だけを落とし、内側は触らない。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("  192.0.2.10  ", "192.0.2.10", id="前後の空白"),
            pytest.param("\t127.0.0.1\t", "127.0.0.1", id="タブ"),
            pytest.param("\n 127.0.0.1 \n", "127.0.0.1", id="改行"),
            pytest.param("", "", id="空文字"),
            pytest.param("   ", "", id="空白のみ"),
            pytest.param("127.0.0.1", "127.0.0.1", id="空白なし"),
            pytest.param("a b", "a b", id="内側の空白は残す"),
        ],
    )
    def test_落とすのは前後の空白だけ(self, tmp_path: Path, value: str, expected: str) -> None:
        # 前後の空白が残っていないことを見たいので、マーカーで囲んでから比較する。
        result = _run_lib(_capture('trim_spaces "$1"'), value, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == f"[{expected}]"


class TestReadBindHostFromEnvFile:
    """`read_bind_host_from_env_file`: 設定ファイルから `BIND_HOST` だけを拾う。"""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param("BIND_HOST=192.0.2.10\n", "192.0.2.10", id="素の値"),
            pytest.param("export BIND_HOST=192.0.2.10\n", "192.0.2.10", id="export付き"),
            pytest.param('BIND_HOST="192.0.2.10"\n', "192.0.2.10", id="ダブルクォート"),
            pytest.param("BIND_HOST='192.0.2.10'\n", "192.0.2.10", id="シングルクォート"),
            pytest.param("BIND_HOST=192.0.2.10  # 公開する\n", "192.0.2.10", id="行末コメント"),
            pytest.param("BIND_HOST=192.0.2.10   \n", "192.0.2.10", id="末尾の空白"),
            pytest.param("BIND_HOST = 192.0.2.10\n", "192.0.2.10", id="等号まわりの空白"),
            pytest.param("  BIND_HOST=192.0.2.10\n", "192.0.2.10", id="行頭のインデント"),
            pytest.param("BIND_HOST=192.0.2.10\r\n", "192.0.2.10", id="CRLF"),
            pytest.param("#BIND_HOST=192.0.2.10\n", "", id="コメントアウト"),
            pytest.param("# BIND_HOST=192.0.2.10\n", "", id="空白付きのコメントアウト"),
            pytest.param("BIND_HOST=\n", "", id="値が空"),
            pytest.param("BIND_HOST=   \n", "", id="値が空白のみ"),
            pytest.param("OTHER=1\n", "", id="キーが無い"),
            pytest.param("", "", id="空のファイル"),
            pytest.param("MY_BIND_HOST=192.0.2.10\n", "", id="別キーの部分一致は拾わない"),
            pytest.param("FOO=bar; BIND_HOST=192.0.2.10\n", "", id="1行へ詰めた形は読まない"),
            pytest.param(
                "BIND_HOST=192.0.2.10\nBIND_HOST=127.0.0.1\n", "127.0.0.1", id="最後の行が勝つ"
            ),
        ],
    )
    def test_設定ファイルの書き方ごとの読み取り(
        self, tmp_path: Path, content: str, expected: str
    ) -> None:
        env_file = _write_env_file(tmp_path, content)

        result = _run_lib(
            _capture("read_bind_host_from_env_file"),
            cwd=tmp_path,
            env={"ENV_FILE": str(env_file)},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == f"[{expected}]"

    def test_ファイルが無くても落ちない(self, tmp_path: Path) -> None:
        """worktree を作った直後は設定ファイルがまだ無い（Issue #65 の CRITICAL）。

        `set -e` と `pipefail` の下で失敗を伝播させると、起動確認どころではなくなる。
        """
        result = _run_lib(
            _capture("read_bind_host_from_env_file"),
            cwd=tmp_path,
            env={"ENV_FILE": str(tmp_path / "存在しない")},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[]"

    def test_読めないファイルでも落ちない(self, tmp_path: Path) -> None:
        env_file = _write_env_file(tmp_path, "BIND_HOST=192.0.2.10\n")
        env_file.chmod(0o000)
        try:
            if os.access(env_file, os.R_OK):
                # root で実行している場合は権限で弾かれない。判定できないので飛ばす。
                # skip も例外なので、権限を戻す処理はこの try の中に入れておく。
                pytest.skip("パーミッションによる読み取り不可を再現できない")

            result = _run_lib(
                _capture("read_bind_host_from_env_file"),
                cwd=tmp_path,
                env={"ENV_FILE": str(env_file)},
            )
        finally:
            env_file.chmod(0o600)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[]"

    def test_既定の設定ファイルは作業ディレクトリから見る(self, tmp_path: Path) -> None:
        """`ENV_FILE` を渡さない場合は、呼び出し元がいるディレクトリの設定ファイル。"""
        (tmp_path / ".env").write_text("BIND_HOST=192.0.2.10\n", encoding="utf-8")

        result = _run_lib(_capture("read_bind_host_from_env_file"), cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[192.0.2.10]"


class TestExpectedBindHost:
    """`expected_bind_host`: 環境変数 → 設定ファイル → 既定値の順に決まる。"""

    def test_環境変数を優先する(self, tmp_path: Path) -> None:
        env_file = _write_env_file(tmp_path, "BIND_HOST=192.0.2.10\n")

        result = _run_lib(
            _capture("expected_bind_host"),
            cwd=tmp_path,
            env={"ENV_FILE": str(env_file), "BIND_HOST": "10.0.0.5"},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[10.0.0.5]"

    def test_環境変数の前後の空白は落とす(self, tmp_path: Path) -> None:
        result = _run_lib(
            _capture("expected_bind_host"),
            cwd=tmp_path,
            env={"ENV_FILE": str(tmp_path / "存在しない"), "BIND_HOST": "  10.0.0.5  "},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[10.0.0.5]"

    @pytest.mark.parametrize(
        "bind_host",
        [pytest.param("", id="空"), pytest.param("   ", id="空白のみ")],
    )
    def test_環境変数が空なら設定ファイルへ落ちる(self, tmp_path: Path, bind_host: str) -> None:
        """`check.sh` は設定ファイルを読まずに呼ぶ（Issue #65）。

        環境変数だけを見ると、設定ファイルで広げている運用に対して呼ぶたび食い違い
        扱いになり、正しい状態に警告が出続ける。
        """
        env_file = _write_env_file(tmp_path, "BIND_HOST=192.0.2.10\n")

        result = _run_lib(
            _capture("expected_bind_host"),
            cwd=tmp_path,
            env={"ENV_FILE": str(env_file), "BIND_HOST": bind_host},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[192.0.2.10]"

    def test_どちらも無ければ既定値(self, tmp_path: Path) -> None:
        result = _run_lib(
            _capture("expected_bind_host"),
            cwd=tmp_path,
            env={"ENV_FILE": str(tmp_path / "存在しない")},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "[127.0.0.1]"

    def test_設定ファイルが無くても落ちない(self, tmp_path: Path) -> None:
        """設定ファイル不在で `set -e` の下でも最後まで進むこと（Issue #65）。"""
        result = _run_lib(
            'expected_bind_host >/dev/null; printf "ok"',
            cwd=tmp_path,
            env={"ENV_FILE": str(tmp_path / "存在しない")},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"


class TestWarnIfPublishedHostDiffers:
    """`warn_if_published_host_differs`: 起動中のコンテナの公開先と設定を突き合わせる。"""

    def _run(
        self,
        tmp_path: Path,
        fake_docker_bin: Path,
        *,
        published: str,
        bind_host: str = "127.0.0.1",
        docker_reachable: str = "yes",
        docker_exit: str = "0",
    ) -> subprocess.CompletedProcess[str]:
        return _run_lib(
            'warn_if_published_host_differs; printf "ok"',
            cwd=tmp_path,
            env={
                "PATH": _path_with(fake_docker_bin),
                "ENV_FILE": str(tmp_path / "存在しない"),
                "COMPOSE_FILE": str(tmp_path / "docker-compose.yml"),
                "BIND_HOST": bind_host,
                "_DOCKER_REACHABLE": docker_reachable,
                "FAKE_DOCKER_PS_OUTPUT": published,
                "FAKE_DOCKER_EXIT": docker_exit,
            },
        )

    def test_一致していれば何も言わない(self, tmp_path: Path, fake_docker_bin: Path) -> None:
        result = self._run(tmp_path, fake_docker_bin, published="127.0.0.1\n")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        assert result.stderr == ""

    def test_composeへ渡す引数を固定する(self, tmp_path: Path, fake_docker_bin: Path) -> None:
        """設定ファイルの指定・サービス名・出力の形を落としても気付けるようにする。

        偽の `docker` は引数を見ないため、これを見ておかないと `-f` が抜けても
        サービス名が変わっても出力テンプレートが壊れてもテストは通ってしまう。
        """
        calls = tmp_path / "docker-calls"
        compose_file = tmp_path / "docker-compose.yml"

        result = _run_lib(
            'warn_if_published_host_differs; printf "ok"',
            cwd=tmp_path,
            env={
                "PATH": _path_with(fake_docker_bin),
                "ENV_FILE": str(tmp_path / "存在しない"),
                "COMPOSE_FILE": str(compose_file),
                "BIND_HOST": "127.0.0.1",
                "_DOCKER_REACHABLE": "yes",
                "FAKE_DOCKER_PS_OUTPUT": "127.0.0.1\n",
                "FAKE_DOCKER_CALLS": str(calls),
            },
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        recorded = calls.read_text(encoding="utf-8")
        # 出力テンプレートに改行を含むため、記録は1回の呼び出しでも複数行になる。
        # 呼び出し回数は `ps` の出現数で数える（一時ディレクトリ名にテスト名が入るため、
        # パスを含む記録全体から "compose" を数えると水増しされる）。
        assert recorded.count(" ps --format ") == 1
        assert recorded.startswith(f"compose -f {compose_file} ps --format ")
        assert "{{range .Publishers}}{{.URL}}" in recorded
        assert "{{end}}" in recorded
        assert recorded.rstrip("\n").endswith(" postgres")

    def test_公開先が複数でも全て一致なら何も言わない(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        result = self._run(tmp_path, fake_docker_bin, published="127.0.0.1\n127.0.0.1\n")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        assert result.stderr == ""

    def test_食い違えば警告と対処を出す(self, tmp_path: Path, fake_docker_bin: Path) -> None:
        result = self._run(tmp_path, fake_docker_bin, published="192.0.2.10\n")

        assert result.returncode == 0, result.stderr
        assert "警告" in result.stderr
        # どちらが公開先でどちらが設定かを取り違えていないところまで見る。入れ替わって
        # いても警告自体は出るため、含まれるかどうかだけでは読み手を誤らせる文言を通す。
        assert "が 192.0.2.10 へ公開されています（設定は 127.0.0.1）" in result.stderr
        assert "./run.sh --stop" in result.stderr

    @pytest.mark.parametrize(
        "published",
        [
            pytest.param("127.0.0.1\n192.0.2.10\n", id="食い違いが最後の行"),
            pytest.param("192.0.2.10\n127.0.0.1\n", id="食い違いが最初の行"),
            pytest.param("127.0.0.1\n192.0.2.10\n127.0.0.1\n", id="食い違いが中間の行"),
        ],
    )
    def test_複数の公開先のうち1つでも食い違えば警告する(
        self, tmp_path: Path, fake_docker_bin: Path, published: str
    ) -> None:
        """食い違いの位置を変えて、全ての行を見ていることを確かめる。

        最後の行だけを見る実装（`tail -1` 相当）でも、最初と最後だけを見る実装でも
        通らないよう、3通りの並びを与える。広く公開されている行が先頭や中間に来る
        並びは実際に起こりうる。
        """
        result = self._run(tmp_path, fake_docker_bin, published=published)

        assert result.returncode == 0, result.stderr
        assert "警告" in result.stderr
        assert "192.0.2.10" in result.stderr

    def test_全インターフェースへの公開も食い違いとして扱う(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """閉じたつもりで開いたまま、という実運用で一番困る形（Issue #65）。"""
        result = self._run(tmp_path, fake_docker_bin, published=f"{_ALL_INTERFACES}\n")

        assert result.returncode == 0, result.stderr
        assert "警告" in result.stderr
        assert _ALL_INTERFACES in result.stderr

    @pytest.mark.parametrize(
        ("expected", "published"),
        [
            pytest.param("127.0.0.1", "127.0.0.10\n", id="前方一致で見逃さない"),
            pytest.param(_ALL_INTERFACES, "10.0.0.0\n", id="後方一致で見逃さない"),
            pytest.param("127.0.0.1", "127.0.0\n", id="公開先が設定値の一部でも見逃さない"),
        ],
    )
    def test_公開先の判定は完全一致(
        self, tmp_path: Path, fake_docker_bin: Path, expected: str, published: str
    ) -> None:
        """部分一致だと `127.0.0.1` が `127.0.0.10` に一致して見逃す（Issue #65）。

        包含の向きも両方見る。設定が公開先を含む形（`127.0.0.1` に対して `127.0.0`）で
        判定する実装へ書き換わっても、片方向だけでは気付けない。
        """
        result = self._run(tmp_path, fake_docker_bin, published=published, bind_host=expected)

        assert result.returncode == 0, result.stderr
        assert "警告" in result.stderr
        assert published.strip() in result.stderr

    def test_dockerへ到達できなければ確認できなかったと言う(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """黙って通さない。警告が無い＝閉じている、と読まれると却って危うくなる。"""
        result = self._run(
            tmp_path, fake_docker_bin, published="192.0.2.10\n", docker_reachable="no"
        )

        assert result.returncode == 0, result.stderr
        assert "確認できませんでした" in result.stderr
        assert "docker" in result.stderr
        assert "警告" not in result.stderr

    @pytest.mark.parametrize(
        ("published", "docker_exit"),
        [
            pytest.param("", "0", id="出力が空"),
            pytest.param("\n \n", "0", id="空白だけ"),
            pytest.param("", "1", id="composeが失敗"),
        ],
    )
    def test_composeから取れなければ確認できなかったと言う(
        self, tmp_path: Path, fake_docker_bin: Path, published: str, docker_exit: str
    ) -> None:
        result = self._run(tmp_path, fake_docker_bin, published=published, docker_exit=docker_exit)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        assert "確認できませんでした" in result.stderr
        assert "警告" not in result.stderr


class TestDockerIsReachable:
    """`docker_is_reachable`: 一度確かめたら覚えておく。"""

    def _run(
        self,
        snippet: str,
        tmp_path: Path,
        fake_docker_bin: Path,
        *,
        docker_exit: str = "0",
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        calls = tmp_path / "docker-calls"
        run_env = {
            "PATH": _path_with(fake_docker_bin),
            "FAKE_DOCKER_EXIT": docker_exit,
            "FAKE_DOCKER_CALLS": str(calls),
        }
        run_env.update(env or {})
        return _run_lib(snippet, cwd=tmp_path, env=run_env), calls

    def test_到達できるときはdocker_infoを一度しか呼ばない(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        result, calls = self._run(
            'docker_is_reachable && docker_is_reachable && docker_is_reachable && printf "ok"',
            tmp_path,
            fake_docker_bin,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        assert calls.read_text(encoding="utf-8").splitlines() == ["info"]

    def test_到達できないときも一度しか呼ばない(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        result, calls = self._run(
            'docker_is_reachable || docker_is_reachable || docker_is_reachable || printf "ng"',
            tmp_path,
            fake_docker_bin,
            docker_exit="1",
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ng"
        assert calls.read_text(encoding="utf-8").splitlines() == ["info"]

    @pytest.mark.parametrize(
        ("memo", "expected"),
        [pytest.param("yes", "ok", id="到達済み"), pytest.param("no", "ng", id="到達不可")],
    )
    def test_覚えている値があればdockerを呼ばない(
        self, tmp_path: Path, fake_docker_bin: Path, memo: str, expected: str
    ) -> None:
        result, calls = self._run(
            'if docker_is_reachable; then printf "ok"; else printf "ng"; fi',
            tmp_path,
            fake_docker_bin,
            env={"_DOCKER_REACHABLE": memo},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == expected
        assert not calls.exists()


class TestAssertDockerUsable:
    """`assert_docker_usable`: docker が要る場面で、未インストールと到達不可を弾く。"""

    def test_未インストールなら目的を添えて落ちる(self, tmp_path: Path) -> None:
        """何のために docker が要るのかを、落ちるときのメッセージへ残す。"""
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()

        result = _run_lib(
            'assert_docker_usable "$1"',
            "PostgreSQLの起動",
            cwd=tmp_path,
            env={"PATH": str(empty_bin)},
        )

        assert result.returncode == 1
        assert "docker未インストール" in result.stderr
        assert "PostgreSQLの起動に必要です" in result.stderr

    def test_到達できるなら何も言わない(self, tmp_path: Path, fake_docker_bin: Path) -> None:
        result = _run_lib(
            'assert_docker_usable "$1"; printf "ok"',
            "PostgreSQLの起動",
            cwd=tmp_path,
            env={"PATH": _path_with(fake_docker_bin), "FAKE_DOCKER_EXIT": "0"},
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        assert result.stderr == ""

    def test_到達できなければ到達不可の案内へ委ねる(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """未インストールではないので、判定は `assert_docker_reachable` の側に出る。"""
        result = _run_lib(
            'assert_docker_usable "$1"',
            "PostgreSQLの起動",
            cwd=tmp_path,
            env={
                "PATH": _path_with(fake_docker_bin),
                "FAKE_DOCKER_EXIT": "1",
                "FAKE_DOCKER_STDERR": _PERMISSION_DENIED,
                "ENTRYPOINT_SCRIPT": "./run.sh",
            },
        )

        assert result.returncode == 1
        assert "docker未インストール" not in result.stderr
        assert "dockerへ接続できません" in result.stderr


class TestAssertDockerReachable:
    """`assert_docker_reachable`: 到達できないとき、そのまま実行できる案内を出す。

    案内は読んだ人がコピーして実行する。埋め込みが壊れると、ライブラリ自身のパスを
    案内したり（実行しても何も起きない）、`./run.sh --stop` が `./run.sh` になったり
    （停止のつもりで起動する）するため、文言ごと固定する（Issue #52 → #55、#70）。

    埋め込む値の安全性も、このクラスで見る。`sg` は受け取った文字列を `/bin/sh` で
    実行するため、引用が1語に収まっていても中身は改めて解釈される。特殊な文字を
    含むときに一行の案内を出さないことと、出すときはそれを実行しても呼び出し元の
    スクリプトしか動かないことを固定する（Issue #71）。
    """

    def _run(
        self,
        tmp_path: Path,
        fake_docker_bin: Path,
        *,
        docker_stderr: str = "",
        docker_exit: str = "1",
        entrypoint_script: str | None = None,
        entrypoint_args: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": _path_with(fake_docker_bin),
            "FAKE_DOCKER_EXIT": docker_exit,
            "FAKE_DOCKER_STDERR": docker_stderr,
        }
        if entrypoint_script is not None:
            env["ENTRYPOINT_SCRIPT"] = entrypoint_script
        if entrypoint_args is not None:
            env["ENTRYPOINT_ARGS"] = entrypoint_args
        return _run_lib('assert_docker_reachable; printf "ok"', cwd=tmp_path, env=env)

    def test_到達できるなら何も言わない(self, tmp_path: Path, fake_docker_bin: Path) -> None:
        result = self._run(tmp_path, fake_docker_bin, docker_exit="0")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "ok"
        assert result.stderr == ""

    def test_権限で弾かれたら入り直し方まで示す(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        result = self._run(
            tmp_path,
            fake_docker_bin,
            docker_stderr=_PERMISSION_DENIED,
            entrypoint_script="./run.sh",
            entrypoint_args="--stop",
        )

        assert result.returncode == 1
        assert "docker group" in result.stderr
        assert "newgrp docker" in result.stderr
        # 案内はそのまま実行される。引数まで含めて崩れていないことを見る。
        assert "sg docker -c './run.sh --stop'" in result.stderr
        # docker group を勧める以上、それが何を意味するかも一緒に出す。
        assert "root相当" in result.stderr
        assert _PERMISSION_DENIED in result.stderr

    @pytest.mark.parametrize(
        "hostile_args",
        [
            pytest.param('--stop" ; echo broken ; :"', id="ダブルクォートで抜け出す"),
            pytest.param("--stop' ; echo broken ; :'", id="シングルクォートで抜け出す"),
            pytest.param("--stop$(echo broken)", id="コマンド置換"),
            pytest.param("--stop`echo broken`", id="バッククォート"),
            pytest.param("--stop ; echo broken", id="セミコロンで区切る"),
            pytest.param("--stop && echo broken", id="ANDで繋ぐ"),
            pytest.param("--stop | echo broken", id="パイプで繋ぐ"),
            pytest.param("--stop\necho broken", id="改行で区切る"),
            pytest.param("--stop $HOME", id="変数展開"),
            pytest.param("--stop *", id="グロブ"),
            pytest.param("--停止", id="非ASCII"),
        ],
    )
    def test_特殊な文字を含む引数は一行の案内に載せない(
        self, tmp_path: Path, fake_docker_bin: Path, hostile_args: str
    ) -> None:
        """`sg docker -c <文字列>` の文字列は `/bin/sh` で実行される（man sg）。

        引用を正しく付けても、引用が解けた後の中身は改めてコマンドとして解釈される。
        そのまま実行できる一行を示すのは、中身が安全な文字だけのときに限る
        （Issue #71）。それ以外は入り直す手順だけを案内する。
        """
        result = self._run(
            tmp_path,
            fake_docker_bin,
            docker_stderr=_PERMISSION_DENIED,
            entrypoint_script="./run.sh",
            entrypoint_args=hostile_args,
        )

        assert result.returncode == 1
        assert _HINT_MARKER not in result.stderr
        # 危険な値そのものを案内へ載せない（コピーされる余地を残さない）。
        assert hostile_args not in result.stderr
        assert "そのまま実行できる形では示しません" in result.stderr
        # 入り直す手そのものは、いずれにせよ示す。
        assert "newgrp docker" in result.stderr
        assert "root相当" in result.stderr

    def test_安全な引数の案内はそのまま実行しても余計なことをしない(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """案内を `sg` が実行するところまで通して、動くものが1つだけであることを見る。

        引用が1語に収まっているかを見るだけでは、`sg` の内側でのコマンド解釈を
        捕まえられない。ここでは案内文の中身を実際に `sh -c` へ渡し、呼び出し元の
        スクリプトだけが動くことを確かめる（実行するのはこのテストが用意した
        無害なスクリプト）。
        """
        entry = tmp_path / "entry.sh"
        entry.write_text(
            '#!/usr/bin/env bash\nprintf "entry:[%s]" "$*"\n',
            encoding="utf-8",
        )
        entry.chmod(0o700)

        result = self._run(
            tmp_path,
            fake_docker_bin,
            docker_stderr=_PERMISSION_DENIED,
            entrypoint_script="./entry.sh",
            entrypoint_args="--stop",
        )

        assert result.returncode == 1
        assert _hint_words(result.stderr) == ["./entry.sh --stop"]

        executed = _simulate_sg(result.stderr, tmp_path)

        assert executed.returncode == 0, executed.stderr
        # 呼び出し元が1回、引数ひとつで動いただけ。別のコマンドは動いていない。
        assert executed.stdout == "entry:[--stop]"

    def test_dockerを呼ぶときはロケールを固定する(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """判定の安定のために `LC_ALL=C` を `docker` まで届ける。

        許可文字の判定に使う文字クラスの範囲指定は照合順序に左右される。docker 側の
        メッセージも英語で揃えば `permission denied` の判定が安定する。`local` だけでは
        呼び出し元が export 済みのときしか子プロセスへ渡らないため、`-x` が要る
        （実測で確認した）。ここでは実行中のシェルの `LC_ALL` を落としてから呼ぶ。
        """
        dump = tmp_path / "docker-env"

        result = _run_lib(
            "assert_docker_reachable || true",
            cwd=tmp_path,
            env={
                "PATH": _path_with(fake_docker_bin),
                "FAKE_DOCKER_EXIT": "0",
                "FAKE_DOCKER_ENV_DUMP": str(dump),
            },
        )

        assert result.returncode == 0, result.stderr
        assert dump.read_text(encoding="utf-8").splitlines() == ["LC_ALL=C"]

    def test_呼び出し元のパスに特殊な文字があっても一行の案内を出さない(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """特殊な文字が入るのは引数側とは限らない。

        パス側だけが原因のときも、案内を出さない側へ倒れる。案内の文言も原因を
        引数と決めつけない（読み手が自分の打った引数を疑って迷う）。
        """
        result = self._run(
            tmp_path,
            fake_docker_bin,
            docker_stderr=_PERMISSION_DENIED,
            entrypoint_script="./起動.sh",
        )

        assert result.returncode == 1
        assert _HINT_MARKER not in result.stderr
        assert "./起動.sh" not in result.stderr
        assert "実行しようとしたコマンド" in result.stderr

    def test_引数が無ければスクリプトだけを案内する(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """`check.sh` は引数を渡さない。余計なものが付かないことを見る。"""
        result = self._run(
            tmp_path,
            fake_docker_bin,
            docker_stderr=_PERMISSION_DENIED,
            entrypoint_script="./scripts/ai-harness/check.sh",
        )

        assert result.returncode == 1
        assert "sg docker -c './scripts/ai-harness/check.sh'" in result.stderr

    def test_スクリプトが無くても引数だけは案内へ残る(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """`ENTRYPOINT_ARGS` だけを渡した組み合わせ。

        いまの呼び出し元（`run.sh` / `check.sh`）はどちらも `ENTRYPOINT_SCRIPT` を
        必ず設定するため実運用では起きない。片方だけ渡したときに案内が壊れないことを
        見ておく（引数が黙って消えると、停止のつもりで起動する案内になる）。
        """
        result = self._run(
            tmp_path,
            fake_docker_bin,
            docker_stderr=_PERMISSION_DENIED,
            entrypoint_args="--stop",
        )

        assert result.returncode == 1
        assert f"sg docker -c '{_SHELL_ARGV0} --stop'" in result.stderr

    def test_呼び出し元が分からなければシェル自身へ落ちる(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """`ENTRYPOINT_SCRIPT` を渡し忘れると `$0` になる。

        ライブラリ自身のパスではなく、呼び出し元のシェルが出る。案内としては弱いが、
        `${BASH_SOURCE[0]}` を見るよりはましという判断（ライブラリのパスを案内しても
        実行しようがない）。その判断ごと固定する。
        """
        result = self._run(tmp_path, fake_docker_bin, docker_stderr=_PERMISSION_DENIED)

        assert result.returncode == 1
        assert f"sg docker -c '{_SHELL_ARGV0}'" in result.stderr
        assert "postgres.sh" not in result.stderr

    def test_権限以外のエラーでは入り直しを勧めない(
        self, tmp_path: Path, fake_docker_bin: Path
    ) -> None:
        """デーモンが落ちているだけのときに docker group を疑わせない。"""
        error = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        result = self._run(
            tmp_path, fake_docker_bin, docker_stderr=error, entrypoint_script="./run.sh"
        )

        assert result.returncode == 1
        assert "dockerへ接続できません" in result.stderr
        assert error in result.stderr
        assert "newgrp docker" not in result.stderr
        assert "sg docker" not in result.stderr
