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
    """パスを「テスト / 実装 / その他」へ分ける。"""

    @pytest.mark.parametrize(
        "path",
        [
            "backend/tests/test_collectors_service.py",
            "backend/tests/conftest.py",
            "frontend/src/lib/api.test.ts",
            "frontend/src/app/page.test.tsx",
            "frontend/vitest.global-setup.test.ts",
            "frontend/eslint-rules/require-test-timeout.test.mjs",
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
            "frontend/src/lib/api.ts",
            "frontend/src/components/features/ArticleCard.tsx",
            "frontend/eslint-rules/require-test-timeout.mjs",
            "scripts/ai-harness/check.sh",
            "scripts/cleanup-test-databases.sh",
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
