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
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "ai-harness" / "lib" / "postgres.sh"

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
        "FAKE_DOCKER_EXIT",
        "FAKE_DOCKER_CALLS",
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
        ["bash", "-euo", "pipefail", "-c", script, "bash", *args],  # noqa: S607
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


def _write_env_file(tmp_path: Path, content: str) -> Path:
    """設定ファイルを作る。`newline=""` で改行をそのまま書き、CRLF を再現する。"""
    path = tmp_path / "env-file"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


@pytest.fixture
def fake_docker_bin(tmp_path: Path) -> Path:
    """`PATH` の先頭へ置く偽の `docker` を作り、そのディレクトリを返す。

    引数は見ずに `FAKE_DOCKER_PS_OUTPUT` を出して `FAKE_DOCKER_EXIT` で終わる。
    `FAKE_DOCKER_CALLS` が指定されていれば呼び出しを1行ずつ追記する
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
        'printf "%s" "${FAKE_DOCKER_PS_OUTPUT:-}"\n'
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
