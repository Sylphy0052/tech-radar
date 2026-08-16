"""`scripts/ai-harness/lib/mr-diff-scope.sh` の関数のテスト（Issue #112）。

このライブラリは「MR の diff にテストの変更しか無い（実装が伴っていない）」状態を
機械的に見つけるために使う。

Issue #109 では、RED のテスト404行だけを含む MR が `Closes #109` 付きで main へ
マージされ、Issue が close された。GREEN の実装は書かれていたが、MR を作った
ブランチの派生元が RED commit の時点のままだったため diff に入らなかった。結果として
main は pytest の収集段階で落ちる状態になり、誰も気付かないまま残った（Issue #111）。

このリポジトリには、これを検出する仕組みが無い。commit 前の `check.sh` 自動実行は
廃止済み（Issue #76）、MR の CI も停止済み（Issue #82）、`gitlab-mr-review` の self
review は diff を読むが「diff に**無い**もの」は見ない。RED のテストだけの diff は、
テスト単体としては整合しているため指摘に上がらない。

判定は「変更ファイルの一覧」だけを入力にする。`glab` や `git` の呼び出しは
`check-mr-scope.sh` 側に置き、ここでは文字列だけを受け取る。外部コマンドが要ると
テストが実際の MR やネットワークに依存してしまうため。

固定するのは次の3つ。

- パスの分類（テスト / 実装 / その他）。実装ファイルの置き場が増えたときに、判定側の
  一覧が追随していなければここが落ちる
- 「テストの変更があり、実装の変更が1つも無い」ときだけ警告になること。ドキュメント
  だけの MR や、実装を伴う MR では警告しない
- Issue #109 と #111 の実際のファイル一覧で、前者が警告・後者が非警告になること

`TestCheckMrScopeWrapper` はラッパー本体（`check-mr-scope.sh`）の統合テスト。
self review（Issue #112 の MR !139）で、ラッパー側の glab/jq 呼び出しに2件の不具合
が見つかった（`.changes` を持たない応答でのフェイルオープン、`--stdin` の末尾改行
欠落）。どちらもライブラリ関数の単体テストだけでは検出できなかったため、ここで
ラッパーを直接起動して固定する。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "ai-harness" / "lib" / "mr-diff-scope.sh"
_WRAPPER = _REPO_ROOT / "scripts" / "ai-harness" / "check-mr-scope.sh"

_BASH = shutil.which("bash") or "/bin/bash"
_SHELL_ARGV0 = "./test-entrypoint"

# このライブラリは `log` / `fail` を定義せず呼び出し元のものを使う（`machine-load.sh`
# と同じ流儀）。テストでも同じ前提で source の前に定義する。
_PREAMBLE = """
log() { printf '%s\\n' "$*" >&2; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
"""

# `diff_lacks_implementation` の終了コードを取り出す断片。`set -e` の下で素のまま
# 呼ぶと非ゼロの時点でシェルが終わり、その後の `printf` へ到達しない。
_CAPTURE_RC = 'rc=0; diff_lacks_implementation "$@" || rc=$?; printf "rc=%s" "$rc"'

# Issue #109 の MR が実際に含んでいた変更（これが素通りした）。
_ISSUE_109_PATHS = ("backend/tests/test_collectors_feed_novelty.py",)

# Issue #111 の MR（#109 の欠落を直したもの）が含んでいた変更。
_ISSUE_111_PATHS = (
    ".secrets.baseline",
    "backend/migrations/versions/20260817_0f3a7c81b2d4_replace_stale_with_no_new_entry_columns.py",
    "backend/src/techradar/collectors/base.py",
    "backend/src/techradar/collectors/discovery.py",
    "backend/src/techradar/collectors/rss.py",
    "backend/src/techradar/collectors/service.py",
    "backend/src/techradar/db/models.py",
    "backend/tests/test_collectors_discovery.py",
    "backend/tests/test_collectors_feed_novelty.py",
    "backend/tests/test_collectors_rss.py",
    "backend/tests/test_collectors_service.py",
    "docs/adr/0008-feed-staleness-detection.md",
)


def _run_lib(snippet: str, *args: str) -> subprocess.CompletedProcess[str]:
    """ライブラリを読み込んだ `set -euo pipefail` のシェルで `snippet` を実行する。

    入力値は `snippet` へ埋め込まず位置引数として渡す。埋め込むと、テストしたい値
    （ファイルパス）がそのままコマンドとして解釈されうる。
    """
    script = f"{_PREAMBLE}\nsource {shlex.quote(str(_LIB))}\n{snippet}\n"
    return subprocess.run(  # noqa: S603
        [_BASH, "-euo", "pipefail", "-c", script, _SHELL_ARGV0, *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
        timeout=30,
    )


def _classify(path: str) -> str:
    result = _run_lib('classify_changed_path "$1"', path)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _lacks_implementation(*paths: str) -> bool:
    result = _run_lib(_CAPTURE_RC, *paths)
    assert "rc=" in result.stdout, result.stderr
    return result.stdout.strip() == "rc=0"


class TestClassifyChangedPath:
    """パスを「テスト / 実装 / その他」へ分ける。

    テスト判定はファイル名の規約とテスト専用ディレクトリだけで行い、`backend/tests/`
    というディレクトリ名では判定しない（self review 指摘、Issue #112）。理由は
    `backend/tests/conftest.py` や `backend/tests/db_process_isolation.py` のように、
    テストディレクトリに判定ロジックそのものが実装として同居しているため。ディレクトリ
    名だけで test に倒すと、この種の実装ファイルの変更が「実装0件」に埋もれて見えなく
    なり、実装が入っているのに警告してしまう（このリポジトリ自身が誤検知の実例）。
    """

    @pytest.mark.parametrize(
        "path",
        [
            "backend/tests/test_collectors_service.py",
            "frontend/src/lib/api.test.ts",
            "frontend/src/app/page.test.tsx",
            "frontend/vitest.global-setup.test.ts",
            "frontend/eslint-rules/require-test-timeout.test.mjs",
            # テスト専用ディレクトリ。ファイル名自体がテストの命名規約に従わなくても
            # テストとして扱う（`test-utils/` 配下の実例）。
            "frontend/src/test-utils/timeouts.ts",
            "frontend/src/test-utils/next-navigation-test-context.tsx",
            "frontend/src/components/__tests__/ArticleCard.test.tsx",
            "__tests__/setup.ts",
            "frontend/src/__mocks__/api.ts",
        ],
    )
    def test_classifies_test_files(self, path: str) -> None:
        assert _classify(path) == "test"

    @pytest.mark.parametrize(
        "path",
        [
            "backend/src/techradar/collectors/service.py",
            "backend/src/techradar/db/models.py",
            "backend/migrations/versions/20260817_0f3a7c81b2d4_replace_stale.py",
            # `backend/tests/` はディレクトリ名では判定しない。ファイル名がテストの
            # 規約に合わないものは実装として数える（CLAUDE.md が「判定ロジックは
            # ここにある」と名指しする2件を含む）。
            "backend/tests/conftest.py",
            "backend/tests/db_process_isolation.py",
            "backend/tests/fake_worktree_roots.py",
            "backend/tests/schema_parity.py",
            "backend/scripts/cleanup_test_databases.py",
            "backend/scripts/requeue_failed_jobs.py",
            "frontend/src/lib/api.ts",
            "frontend/src/components/features/ArticleCard.tsx",
            "frontend/eslint-rules/require-test-timeout.mjs",
            "frontend/vitest.global-setup.ts",
            "frontend/vitest.orphaned-coverage-dirs.ts",
            "frontend/next.config.ts",
            "scripts/ai-harness/check.sh",
            "scripts/cleanup-test-databases.sh",
            "infra/docker-compose.yml",
        ],
    )
    def test_classifies_implementation_files(self, path: str) -> None:
        assert _classify(path) == "impl"

    @pytest.mark.parametrize(
        "path",
        [
            "docs/adr/0008-feed-staleness-detection.md",
            "CLAUDE.md",
            "README.md",
            ".secrets.baseline",
            ".gitignore",
            "backend/pyproject.toml",
            "frontend/package.json",
        ],
    )
    def test_classifies_other_files(self, path: str) -> None:
        assert _classify(path) == "other"

    def test_test_wins_over_implementation_for_paths_matching_both(self) -> None:
        """`backend/src` 配下にテストが置かれても、テストとして数える。

        分類は「テストか否か」を先に見る。実装のディレクトリ配下にテストがあるとき、
        実装として数えると「実装が入っている」と誤認してこの検査が素通りする。
        """
        assert _classify("backend/src/techradar/test_something.py") == "test"
        assert _classify("frontend/src/lib/articles.test.ts") == "test"

    def test_test_wins_over_frontend_extension_rule_for_paths_matching_both(
        self,
    ) -> None:
        """`frontend/*.ts` は実装だが、テストの命名規約が先に効く。

        `frontend/vitest.global-setup.ts` は実装、`frontend/vitest.global-setup.test.ts`
        はテスト。拡張子ベースの実装判定とテストの命名規約が競合しうるため、この順序
        依存を固定する。
        """
        assert _classify("frontend/vitest.global-setup.ts") == "impl"
        assert _classify("frontend/vitest.global-setup.test.ts") == "test"


class TestDiffLacksImplementation:
    """「テストの変更があり、実装の変更が1つも無い」ときだけ警告する。"""

    def test_warns_when_only_tests_changed(self) -> None:
        assert _lacks_implementation("backend/tests/test_collectors_service.py") is True

    def test_warns_when_tests_and_docs_changed_without_implementation(self) -> None:
        """テストと ADR だけ、という形でも同じ事故になるため警告する。"""
        assert (
            _lacks_implementation(
                "backend/tests/test_collectors_service.py",
                "docs/adr/0008-feed-staleness-detection.md",
            )
            is True
        )

    def test_does_not_warn_when_implementation_is_present(self) -> None:
        assert (
            _lacks_implementation(
                "backend/src/techradar/collectors/service.py",
                "backend/tests/test_collectors_service.py",
            )
            is False
        )

    def test_does_not_warn_for_a_docs_only_change(self) -> None:
        """テストの変更を含まないため、この検査の対象外。"""
        assert _lacks_implementation("docs/adr/0008-feed-staleness-detection.md") is False
        assert _lacks_implementation("CLAUDE.md") is False

    def test_does_not_warn_for_an_implementation_only_change(self) -> None:
        """テストが無いこと自体は別の問題で、この検査は扱わない。"""
        assert _lacks_implementation("backend/src/techradar/collectors/service.py") is False

    def test_does_not_warn_for_an_empty_diff(self) -> None:
        """変更が無い MR は判定の対象にならない（差分0で警告しても意味が無い）。"""
        assert _lacks_implementation() is False

    def test_warns_for_the_issue_109_merge_request(self) -> None:
        """受入基準: 実際に素通りした Issue #109 の MR を捕まえる。"""
        assert _lacks_implementation(*_ISSUE_109_PATHS) is True

    def test_does_not_warn_for_the_issue_111_merge_request(self) -> None:
        """受入基準: #109 の欠落を直した Issue #111 の MR では警告しない。"""
        assert _lacks_implementation(*_ISSUE_111_PATHS) is False


class TestDescribeDiffScope:
    """警告の根拠として内訳を出す。何件がテストで何件が実装かを人が読めるようにする。"""

    def test_reports_the_counts_per_category(self) -> None:
        result = _run_lib(
            'describe_diff_scope "$@"',
            "backend/src/techradar/collectors/service.py",
            "backend/tests/test_collectors_service.py",
            "backend/tests/test_collectors_rss.py",
            "docs/adr/0008-feed-staleness-detection.md",
        )
        assert result.returncode == 0, result.stderr
        assert "テスト 2件" in result.stdout
        assert "実装 1件" in result.stdout
        assert "その他 1件" in result.stdout

    def test_reports_zero_counts_for_an_empty_diff(self) -> None:
        result = _run_lib('describe_diff_scope "$@"')
        assert result.returncode == 0, result.stderr
        assert "テスト 0件" in result.stdout
        assert "実装 0件" in result.stdout
        assert "その他 0件" in result.stdout


class TestCheckMrScopeWrapper:
    """`check-mr-scope.sh` 本体の統合テスト（self review 指摘、Issue #112 の MR !139）。

    ライブラリ関数の単体テストは「分類」と「判定」しか見ておらず、ラッパー側で
    `glab api` の応答を読む部分は素通りしていた。ここでラッパーを直接起動し、次の
    2件を固定する。

    - `.changes` を持たない応答（認証エラー等）が来たときに、フェイルオープンせず
      rc=2（判定できず）で止まること
    - `--stdin` の入力に末尾改行が無くても、最終行を読み落とさないこと
    """

    def _run_wrapper(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        bin_dir: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if bin_dir is not None:
            # PATH は丸ごと差し替えない。`jq` は本物を使わせる必要があるため、
            # 偽の `glab` を置いたディレクトリを先頭に足すだけにする。
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        return subprocess.run(  # noqa: S603
            [_BASH, str(_WRAPPER), *args],
            input=input_text,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=30,
        )

    def _write_fake_glab(self, tmp_path: Path, script_body: str) -> Path:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        glab = bin_dir / "glab"
        glab.write_text(f"#!/usr/bin/env bash\n{script_body}\n", encoding="utf-8")
        glab.chmod(0o755)
        return bin_dir

    def test_stdin_with_trailing_newline_warns(self) -> None:
        result = self._run_wrapper(["--stdin"], input_text="backend/tests/test_only_red.py\n")
        assert result.returncode == 1, result.stderr

    def test_stdin_without_trailing_newline_warns(self) -> None:
        """実測で欠落していた挙動。末尾改行が無いと最終行が消え、警告が出なかった。"""
        result = self._run_wrapper(["--stdin"], input_text="backend/tests/test_only_red.py")
        assert result.returncode == 1, result.stderr

    def test_stdin_trailing_newline_does_not_change_the_count(self) -> None:
        """末尾改行の有無で件数がずれないこと（片方だけ多く/少なく数えない）。"""
        with_newline = self._run_wrapper(["--stdin"], input_text="backend/tests/test_only_red.py\n")
        without_newline = self._run_wrapper(
            ["--stdin"], input_text="backend/tests/test_only_red.py"
        )
        assert "変更 1件" in with_newline.stderr
        assert "変更 1件" in without_newline.stderr

    def test_stdin_issue_109_list_warns(self) -> None:
        result = self._run_wrapper(["--stdin"], input_text="\n".join(_ISSUE_109_PATHS) + "\n")
        assert result.returncode == 1, result.stderr

    def test_stdin_issue_111_list_does_not_warn(self) -> None:
        result = self._run_wrapper(["--stdin"], input_text="\n".join(_ISSUE_111_PATHS) + "\n")
        assert result.returncode == 0, result.stderr

    def test_stdin_empty_input_does_not_warn(self) -> None:
        result = self._run_wrapper(["--stdin"], input_text="")
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "args",
        [[], ["139", "extra"], ["not-a-number"]],
        ids=["no-args", "two-args", "non-numeric-arg"],
    )
    def test_invalid_arguments_are_a_usage_error(self, args: list[str]) -> None:
        result = self._run_wrapper(args, input_text="")
        assert result.returncode == 2, result.stderr

    def test_glab_success_is_parsed(self, tmp_path: Path) -> None:
        bin_dir = self._write_fake_glab(
            tmp_path,
            "cat <<'JSON'\n"
            '{"changes":[{"new_path":"backend/tests/test_collectors_feed_novelty.py"}]}\n'
            "JSON",
        )
        result = self._run_wrapper(["139"], bin_dir=bin_dir)
        assert result.returncode == 1, result.stderr

    def test_glab_nonzero_exit_fails_closed(self, tmp_path: Path) -> None:
        bin_dir = self._write_fake_glab(tmp_path, "echo 'boom' >&2\nexit 1")
        result = self._run_wrapper(["139"], bin_dir=bin_dir)
        assert result.returncode == 2, result.stderr

    def test_glab_response_without_changes_field_fails_closed(self, tmp_path: Path) -> None:
        """認証エラー等で `{"message": ...}` が返ってきたケース。

        フェイルオープンしていた不具合の本体。`.changes` が無い応答でも
        `jq -r '.changes[]?...'` は空文字を返すだけで exit 0 のため、以前の実装は
        変更0件・rc=0（警告なし）のまま正常終了していた。
        """
        bin_dir = self._write_fake_glab(tmp_path, 'echo \'{"message":"404 Project Not Found"}\'')
        result = self._run_wrapper(["139"], bin_dir=bin_dir)
        assert result.returncode == 2, result.stderr

    def test_glab_non_json_response_fails_closed(self, tmp_path: Path) -> None:
        bin_dir = self._write_fake_glab(tmp_path, "echo 'not json at all'")
        result = self._run_wrapper(["139"], bin_dir=bin_dir)
        assert result.returncode == 2, result.stderr

    def test_glab_path_with_embedded_newline_counts_as_one_file(self, tmp_path: Path) -> None:
        """パスの抽出を NUL 区切りにした理由そのもの。改行を含むパスがコマンド置換や
        改行区切りの読み取りだと2件に分裂し、内訳の件数がずれる。"""
        bin_dir = self._write_fake_glab(
            tmp_path,
            "cat <<'JSON'\n"
            r'{"changes":[{"new_path":"backend/tests/weird\nname.py"}]}'
            "\n"
            "JSON",
        )
        result = self._run_wrapper(["139"], bin_dir=bin_dir)
        assert "変更 1件" in result.stderr, result.stderr
