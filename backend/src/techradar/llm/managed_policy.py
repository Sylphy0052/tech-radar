"""管理者ポリシー（admin-managed settings）の検知。

ポリシーが配布されたホストでは、`claude_cli` が積み重ねている防御はほとんど
機能しない。Issue #56 / #66 の実測で分かっているのは次の4点で、いずれも
コマンドライン引数から無効化できない。

- `env` で `ANTHROPIC_BASE_URL` を差し替えられる。ツールが1つも動かなくても
  記事本文・要約・`authorization` ヘッダが第三者のエンドポイントへ渡る
- `apiKeyHelper` からシステムシェル経由で任意コマンドが走る
- hooks がイベントを問わず実行される。`disableAllHooks: true` を渡しても止まらない
- `claudeMd` でシステムプロンプト相当の指示を注入される

実行を止められる対策はコンテナ隔離だけだが、それまでの間、配布されたことに
気づかないまま記事本文を送り続けるのは避けたい。そこで CLI を起動する前に
配置先を検査し、見つかったら実行しない（フェイルクローズ）。

**判定は存在の有無だけで行い、中身は読まない。** 危険なキーを列挙する形も
考えられるが、列挙式は漏れる。`--tools ""` を主防御に選んだ理由が「列挙では
なく構造的だから新しいツールが増えても漏れない」であり、検知だけ列挙式に
するのは方針として一貫しない。無害なポリシーが配られた端末で動かしたい場合は
`ALLOW_MANAGED_POLICY` で明示的に外す。

詳細は `docs/adr/0002-llm-tool-isolation.md` を参照。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from techradar.config import Settings, get_settings
from techradar.llm.errors import LLMManagedPolicyDetectedError

# ポリシー本体のファイル名と、systemd 風の drop-in ディレクトリ名。
# drop-in だけを置いた状態でも hooks が実行されることを実測済み（Issue #66）。
POLICY_FILE_NAME = "managed-settings.json"
POLICY_DROPIN_DIR_NAME = "managed-settings.d"

# OS ごとの配置先。ここに無いプラットフォームは Linux と同じ場所を見る。
# 見落として素通りさせるより、余分に検査するほうへ倒す。
_POLICY_DIRECTORIES_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "darwin": ("/Library/Application Support/ClaudeCode",),
    "win32": (r"C:\Program Files\ClaudeCode",),
}
_DEFAULT_POLICY_DIRECTORIES: tuple[str, ...] = ("/etc/claude-code",)


def managed_policy_directories(platform: str | None = None) -> tuple[Path, ...]:
    """ポリシーの配置先を返す。

    Args:
        platform: `sys.platform` 相当の値。省略時は実行中のプラットフォーム。
    """
    resolved = platform if platform is not None else sys.platform
    directories = _POLICY_DIRECTORIES_BY_PLATFORM.get(resolved, _DEFAULT_POLICY_DIRECTORIES)
    return tuple(Path(directory) for directory in directories)


def find_managed_policy_files(directories: Iterable[Path]) -> list[Path]:
    """配置先にあるポリシーファイルを列挙する。

    ディレクトリが存在しない場合は何も返さない。drop-in ディレクトリからは
    CLI が読む `*.json` だけを拾う。
    """
    found: list[Path] = []
    for directory in directories:
        policy_file = directory / POLICY_FILE_NAME
        if policy_file.is_file():
            found.append(policy_file)
        dropin = directory / POLICY_DROPIN_DIR_NAME
        found.extend(sorted(path for path in dropin.glob("*.json") if path.is_file()))
    return found


def assert_no_managed_policy(
    settings: Settings | None = None,
    *,
    directories: Iterable[Path] | None = None,
) -> None:
    """ポリシーが配布されていないことを確認する。

    Args:
        settings: 省略時はアプリケーション設定を読む。
        directories: 検査対象。省略時はプラットフォームごとの既定。

    Raises:
        LLMManagedPolicyDetectedError: ポリシーが見つかった場合。
    """
    resolved = settings or get_settings()
    if resolved.allow_managed_policy:
        return

    targets = directories if directories is not None else managed_policy_directories()
    found = find_managed_policy_files(targets)
    if not found:
        return

    listed = ", ".join(str(path) for path in found)
    message = (
        f"管理者ポリシーが配布されています。CLI 側の隔離では防げないため実行しません: {listed}"
    )
    raise LLMManagedPolicyDetectedError(message)
