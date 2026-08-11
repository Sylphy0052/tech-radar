"""管理者ポリシーの検知を固定するテスト（Issue #67）。

ポリシーが配布されたホストでは ADR 0002 の防御がほとんど機能しない
（Issue #56 / #66 の実測）。塞ぐ手はコンテナ隔離しかないため、せめて
気づけるよう起動前に検査する。検知したら CLI を起動しない。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from techradar.config import Settings
from techradar.llm.claude_cli import ClaudeCliProvider
from techradar.llm.errors import LLMManagedPolicyDetectedError
from techradar.llm.managed_policy import (
    assert_no_managed_policy,
    find_managed_policy_files,
    managed_policy_directories,
)
from techradar.llm.retry import NON_RETRYABLE
from tests.test_llm_claude_cli import ArticleSummary

POLICY_FILE_NAME = "managed-settings.json"
DROPIN_DIR_NAME = "managed-settings.d"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class TestManagedPolicyDirectories:
    def test_linuxではetc配下を見る(self) -> None:
        # Arrange / Act
        directories = managed_policy_directories("linux")

        # Assert
        assert Path("/etc/claude-code") in directories

    def test_macosでは専用のパスを見る(self) -> None:
        # Arrange / Act
        directories = managed_policy_directories("darwin")

        # Assert
        assert Path("/Library/Application Support/ClaudeCode") in directories
        assert Path("/etc/claude-code") not in directories

    def test_windowsでは専用のパスを見る(self) -> None:
        # Arrange / Act
        directories = managed_policy_directories("win32")

        # Assert
        assert any("ClaudeCode" in str(directory) for directory in directories)
        assert Path("/etc/claude-code") not in directories

    def test_未知のプラットフォームではlinuxと同じ場所を見る(self) -> None:
        """判定に迷ったら検査する側へ倒す。見落として素通りさせない。"""
        # Arrange / Act
        directories = managed_policy_directories("freebsd14")

        # Assert
        assert Path("/etc/claude-code") in directories


class TestFindManagedPolicyFiles:
    def test_存在しないディレクトリは無視する(self, tmp_path: Path) -> None:
        # Arrange
        missing = tmp_path / "absent"

        # Act
        found = find_managed_policy_files([missing])

        # Assert
        assert found == []

    def test_ポリシーファイルを見つける(self, tmp_path: Path) -> None:
        # Arrange
        policy = tmp_path / POLICY_FILE_NAME
        policy.write_text("{}", encoding="utf-8")

        # Act
        found = find_managed_policy_files([tmp_path])

        # Assert
        assert found == [policy]

    def test_dropinディレクトリのjsonも見つける(self, tmp_path: Path) -> None:
        """`managed-settings.d/` だけでも hooks は読まれる（Issue #66 で実測）。"""
        # Arrange
        dropin = tmp_path / DROPIN_DIR_NAME
        dropin.mkdir()
        first = dropin / "10-hooks.json"
        first.write_text("{}", encoding="utf-8")
        second = dropin / "20-env.json"
        second.write_text("{}", encoding="utf-8")

        # Act
        found = find_managed_policy_files([tmp_path])

        # Assert
        assert found == [first, second]

    def test_空のdropinディレクトリでは検知しない(self, tmp_path: Path) -> None:
        # Arrange
        (tmp_path / DROPIN_DIR_NAME).mkdir()

        # Act
        found = find_managed_policy_files([tmp_path])

        # Assert
        assert found == []

    def test_json以外のファイルは検知しない(self, tmp_path: Path) -> None:
        """CLI が読むのは `*.json` だけ。README などで止めない。"""
        # Arrange
        dropin = tmp_path / DROPIN_DIR_NAME
        dropin.mkdir()
        (dropin / "README.md").write_text("メモ", encoding="utf-8")

        # Act
        found = find_managed_policy_files([tmp_path])

        # Assert
        assert found == []


class TestAssertNoManagedPolicy:
    def test_ポリシーが無ければ何も起きない(self, tmp_path: Path, settings: Settings) -> None:
        # Arrange / Act / Assert
        assert_no_managed_policy(settings, directories=[tmp_path])

    def test_ポリシーがあれば例外を送出する(self, tmp_path: Path, settings: Settings) -> None:
        # Arrange
        policy = tmp_path / POLICY_FILE_NAME
        policy.write_text("{}", encoding="utf-8")

        # Act / Assert
        with pytest.raises(LLMManagedPolicyDetectedError) as exc_info:
            assert_no_managed_policy(settings, directories=[tmp_path])

        # 見つかった場所が分からないと対処できない。
        assert str(policy) in str(exc_info.value)

    def test_中身は読まない(self, tmp_path: Path, settings: Settings) -> None:
        """判定は存在の有無だけで行う。壊れた JSON でも同じように止める。"""
        # Arrange
        policy = tmp_path / POLICY_FILE_NAME
        policy.write_text("これはJSONではない", encoding="utf-8")

        # Act / Assert
        with pytest.raises(LLMManagedPolicyDetectedError):
            assert_no_managed_policy(settings, directories=[tmp_path])

    def test_明示的に許可すれば素通りする(self, tmp_path: Path) -> None:
        """無害なポリシーが配られた端末向けの逃げ道。既定では閉じている。"""
        # Arrange
        (tmp_path / POLICY_FILE_NAME).write_text("{}", encoding="utf-8")
        permissive = Settings(_env_file=None, allow_managed_policy=True)

        # Act / Assert
        assert_no_managed_policy(permissive, directories=[tmp_path])

    def test_既定では許可しない(self, settings: Settings) -> None:
        # Arrange / Act / Assert
        assert settings.allow_managed_policy is False


class TestRetryPolicy:
    def test_リトライ対象にしない(self) -> None:
        """ポリシーは再試行で消えない。"""
        # Arrange / Act / Assert
        assert issubclass(LLMManagedPolicyDetectedError, NON_RETRYABLE)


class TestProviderIntegration:
    def test_ポリシーを検知したらCLIを起動しない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        (tmp_path / POLICY_FILE_NAME).write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "techradar.llm.managed_policy.managed_policy_directories",
            lambda platform=None: (tmp_path,),
        )

        def fail_if_called(*args: object, **kwargs: object) -> None:
            message = "ポリシー検知後に CLI が起動された"
            raise AssertionError(message)

        monkeypatch.setattr(subprocess, "run", fail_if_called)
        provider = ClaudeCliProvider(Settings(_env_file=None))

        # Act / Assert
        with pytest.raises(LLMManagedPolicyDetectedError):
            provider.complete_json(
                instruction="要約せよ",
                untrusted_content="本文",
                schema=ArticleSummary,
            )
