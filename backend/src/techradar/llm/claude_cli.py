"""Claude Code CLI を headless で呼び出す `LLMProvider` 実装。

記事本文は非信頼入力であり、ツールが動くこと自体が隔離の失敗を意味する。
そのためツールの無効化を多層で行い、**さらに実行後の観測でも確認**する。

## ツール無効化について

`--allowedTools ""` はツールを無効化しない。実測で、この指定のみでは
`Read` が実行され `/etc/hostname` の内容が返った（`num_turns` が 2 になる）。
広く使われる書き方だが効果がない。

主防御は `--tools ""` で、CLI のヘルプに「Use "" to disable all tools」と
明記されたフラグ。組み込みツールを列挙ではなく構造的に空にするため、
新しいツールが増えても漏れない。

これに次を重ねる。

1. `--setting-sources ""` で設定ファイル（user / project / local）を読み込ませない。
   hooks はここに定義され、ツール許可とは別経路で任意コマンドを実行しうるため、
   ツール無効化だけでは塞げない。ただし管理者ポリシー（admin-managed policy）は
   この3つに含まれず、ポリシーに定義された hooks は実行される（実測）。
   主防御の `--tools ""` はポリシーからは上書きされない
   （`docs/adr/0002-llm-tool-isolation.md` の残存リスクを参照）
2. `--settings` の `permissions.deny` と `--disallowedTools` にツール名を列挙（保険）
3. `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` で MCP を読み込ませない。
   サーバは空のままにする。ポリシーの `disableSideloadFlags` は `--mcp-config` を
   拒否するが、空の指定だけは受理される（1つでも足すと起動できなくなる）
4. `--disable-slash-commands` で Skills（`/skill-name`）を無効化する。Skills は
   ツール無効化の管轄外にある独立した実行経路で、`--tools ""` でも `--bare` でも残る
5. 環境変数を許可リストで絞り、DB 接続文字列などを子プロセスへ渡さない
6. 一時ディレクトリを cwd にし、実行場所由来の設定を拾わせない
7. `stdin` を閉じ、記事本文を扱うプロセスへ余計な入力経路を残さない

plugin は `--plugin-dir` / `--plugin-url` を渡していないため読み込まれない。

4 を入れているのはプロンプト構造に頼らないためである。プロンプトは常に開発者側の
指示で始まり、記事本文は `build_user_prompt` が `<untrusted_content>` タグに包んで
末尾へ置くため、本文がスラッシュコマンドとして解釈される経路はもともと無い。ただし
それはプロンプトの組み立て方に依存した防御で、本文を先頭付近へ動かすような変更を
入れれば崩れる。フラグの有無で挙動が変わらないことの実測は
`docs/adr/0002-llm-tool-isolation.md` に記録している。

2 は列挙式で漏れうる。そのため実行後に `num_turns` と `permission_denials` を
検査し、ツール使用の兆候があれば結果を採用せず失敗させる。応答からこれらの
フィールドが消えたり型が変わったりした場合も、検査できないまま通さず失敗させる。

`--bare` でも hooks / plugin / CLAUDE.md 自動探索を止められるが採らない。OAuth を
読まなくなり `ANTHROPIC_API_KEY` が必須になるため、サブスク枠で動かすという
ADR 0001 の決定と両立しない（`docs/adr/0002-llm-tool-isolation.md` で却下済み）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any

from pydantic import BaseModel

from techradar.config import Settings, get_settings
from techradar.llm.base import LLMCompletion, LLMUsage, validate_response
from techradar.llm.errors import (
    LLMInvocationError,
    LLMTimeoutError,
    LLMToolUseDetectedError,
)
from techradar.llm.prompt import SYSTEM_PROMPT, build_user_prompt

# `--tools` に渡す値。CLI のヘルプに「Use "" to disable all tools」と明記されており、
# 組み込みツールを列挙ではなく構造的に空にする。新しいツールが増えても漏れない。
NO_TOOLS = ""

# 上の指定が将来効かなくなった場合に備えた二重化。列挙式なので漏れうるが、
# `--tools ""` が主防御であり、これは保険として置く。
# CLI が認識しない名前を渡すと起動時に警告が出て終了コードが 1 になるため、
# 実在するツール名だけを列挙する（`MultiEdit` / `SlashCommand` は存在しない）。
DENIED_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "Bash",
    "BashOutput",
    "KillShell",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "NotebookEdit",
    "TodoWrite",
    "AskUserQuestion",
    "ExitPlanMode",
    "SendUserMessage",
    "ListMcpResources",
    "ReadMcpResource",
)

# MCP サーバーを 1 つも読み込ませない設定。空のままにしておくこと。
# 管理者ポリシーの `disableSideloadFlags` は `--mcp-config` を起動時に拒否するが、
# サーバを含まない指定は受理される。1 つでも足すとポリシー配下のホストで起動できない。
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# 子プロセスへ渡す環境変数。既定では親の環境がすべて継承され、DB 接続文字列などが
# CLI プロセスから見えてしまう。認証と実行に必要なものだけを通す。
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TZ",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def build_environment() -> dict[str, str]:
    """CLI へ渡す環境変数を許可リストで絞る。"""
    return {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}


# 応答がコードフェンスで包まれることがあるため取り除く。
_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """コードフェンスを取り除く。"""
    match = _CODE_FENCE.match(text)
    return match.group(1) if match else text.strip()


def _build_command(settings: Settings, prompt: str) -> list[str]:
    """CLI の起動コマンドを組み立てる。"""
    permission_settings = json.dumps({"permissions": {"deny": list(DENIED_TOOLS)}})
    command = [
        settings.claude_cli_path,
        "--print",
        prompt,
        "--output-format",
        "json",
        "--system-prompt",
        SYSTEM_PROMPT,
        # 組み込みツールを構造的に空にする（主防御）。
        "--tools",
        NO_TOOLS,
        # 設定ファイル（user / project / local）を一切読み込ませない。
        # hooks はここに定義され、ツール許可とは別経路で任意コマンドを実行しうる。
        # 実測で input_tokens が 4076 -> 175 に落ちることから、設定と
        # CLAUDE.md が読み込まれていないことを確認している。
        #
        # `--bare` でも hooks を止められるが、OAuth を読まなくなり
        # ANTHROPIC_API_KEY が必須になるため使わない
        # （サブスク枠で動かすという ADR 0001 の決定と両立しない）。
        "--setting-sources",
        "",
        "--settings",
        permission_settings,
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
        # Skills（`/skill-name`）はツール無効化の管轄外にある独立した実行経路で、
        # `--tools ""` でも `--bare` でも残る。ここで明示的に落とす。
        "--disable-slash-commands",
        "--disallowedTools",
        *DENIED_TOOLS,
    ]
    if settings.claude_cli_model:
        command = [*command, "--model", settings.claude_cli_model]
    return command


def _assert_no_tool_use(envelope: dict[str, Any]) -> None:
    """ツールが使われた形跡がないことを確認する。

    無効化の指定は列挙式で漏れうるため、結果側からも検証する。
    ツールを 1 度も呼ばなければ `num_turns` は 1 になる。
    """
    denials = envelope.get("permission_denials")
    turns = envelope.get("num_turns")

    # 期待するフィールドが無い・型が違う場合は「ツールを使っていない」と
    # みなさず失敗させる。CLI の更新で検知が静かに無効化されるのを防ぐ。
    if not isinstance(turns, int) or not isinstance(denials, list):
        message = (
            "CLI の応答にツール使用を判定できるフィールドがありません: "
            f"num_turns={turns!r}, permission_denials={denials!r}"
        )
        raise LLMInvocationError(message)

    if denials or turns > 1:
        message = f"ツール使用が観測されました: num_turns={turns}, denials={len(denials)}"
        raise LLMToolUseDetectedError(message)


def _parse_envelope(stdout: str) -> dict[str, Any]:
    """CLI の JSON 封筒を読み取る。"""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        message = f"CLI の出力を JSON として解釈できません: {stdout[:200]}"
        raise LLMInvocationError(message) from exc

    if not isinstance(envelope, dict):
        message = "CLI の出力が想定した形式ではありません"
        raise LLMInvocationError(message)

    if envelope.get("is_error"):
        message = f"CLI がエラーを返しました: {envelope.get('subtype')}"
        raise LLMInvocationError(message)

    return envelope


def _extract_usage(envelope: dict[str, Any]) -> LLMUsage:
    """使用モデル・トークン数・処理時間を取り出す（§24 可観測性）。"""
    usage = envelope.get("usage") or {}
    model_usage = envelope.get("modelUsage") or {}
    model = next(iter(model_usage), "unknown")
    return LLMUsage(
        model=model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        duration_ms=int(envelope.get("duration_ms", 0)),
    )


class ClaudeCliProvider:
    """Claude Code CLI を subprocess として呼び出すプロバイダー。"""

    name = "claude-cli"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def complete_json(
        self,
        *,
        instruction: str,
        untrusted_content: str,
        schema: type[BaseModel],
    ) -> LLMCompletion:
        """非信頼テキストから、`schema` で検証済みの JSON を得る。"""
        prompt = build_user_prompt(
            instruction=instruction,
            untrusted_content=untrusted_content,
            schema_hint=json.dumps(schema.model_json_schema(), ensure_ascii=False),
        )
        envelope = self._invoke(prompt)
        _assert_no_tool_use(envelope)

        raw_text = _strip_code_fence(str(envelope.get("result", "")))
        return LLMCompletion(
            data=validate_response(schema, raw_text),
            usage=_extract_usage(envelope),
            raw_text=raw_text,
        )

    def _invoke(self, prompt: str) -> dict[str, Any]:
        """CLI を実行して JSON 封筒を返す。"""
        command = _build_command(self._settings, prompt)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="techradar-llm-") as working_directory:
            return self._run(command, started, working_directory)

    def _run(self, command: list[str], started: float, working_directory: str) -> dict[str, Any]:
        """組み立て済みのコマンドを実行する。"""
        try:
            completed = subprocess.run(  # noqa: S603 — 引数は自前で構築し shell を介さない
                command,
                capture_output=True,
                text=True,
                timeout=self._settings.llm_timeout_seconds,
                check=False,
                # 記事本文を扱うプロセスに標準入力を渡さない。
                stdin=subprocess.DEVNULL,
                # 親の環境をそのまま渡すと DB 接続文字列などが CLI から見える。
                env=build_environment(),
                # 実行ディレクトリ由来の設定を拾わせないため空ディレクトリで動かす。
                cwd=working_directory,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            message = f"CLI が {elapsed} ms でタイムアウトしました"
            raise LLMTimeoutError(message) from exc
        except OSError as exc:
            message = f"CLI を起動できません: {self._settings.claude_cli_path}"
            raise LLMInvocationError(message) from exc

        if completed.returncode != 0:
            message = (
                f"CLI が終了コード {completed.returncode} で終了しました: "
                f"{completed.stderr.strip()[:200]}"
            )
            raise LLMInvocationError(message)

        return _parse_envelope(completed.stdout)
