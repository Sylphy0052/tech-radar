"""Claude Code CLI を headless で呼び出す `LLMProvider` 実装。

記事本文は非信頼入力であり、ツールが動くこと自体が隔離の失敗を意味する。
そのためツールの無効化を多層で行い、**さらに実行後の観測でも確認**する。

## ツール無効化について

`--allowedTools ""` はツールを無効化しない。実測で、この指定のみでは
`Read` が実行され `/etc/hostname` の内容が返った（`num_turns` が 2 になる）。
広く使われる書き方だが効果がないため、次の 3 つを併用する。

1. `--settings` の `permissions.deny` にツール名を列挙する
   （実測で最も強く、ツール呼び出しの試行自体が発生しなくなる）
2. `--disallowedTools` にも同じ一覧を渡す
3. `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` で MCP サーバーを読み込ませない

いずれも**列挙式**であり、CLI に新しいツールが増えると漏れる。
そのため実行後に `num_turns` と `permission_denials` を検査し、
ツール使用の兆候があれば結果を採用せず失敗させる。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from techradar.config import Settings, get_settings
from techradar.llm.base import LLMCompletion, LLMUsage
from techradar.llm.errors import (
    LLMInvalidResponseError,
    LLMInvocationError,
    LLMTimeoutError,
    LLMToolUseDetectedError,
)
from techradar.llm.prompt import SYSTEM_PROMPT, build_user_prompt

# CLI が認識するツール名のみを列挙する。未知の名前を渡すと警告が出るだけで
# 拒否には寄与しないため、実在するものに絞る。
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
    "Artifact",
)

# MCP サーバーを 1 つも読み込ませない設定。
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

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
        "--settings",
        permission_settings,
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
        "--disallowedTools",
        *DENIED_TOOLS,
    ]
    if settings.claude_cli_model:
        command += ["--model", settings.claude_cli_model]
    return command


def _assert_no_tool_use(envelope: dict[str, Any]) -> None:
    """ツールが使われた形跡がないことを確認する。

    無効化の指定は列挙式で漏れうるため、結果側からも検証する。
    ツールを 1 度も呼ばなければ `num_turns` は 1 になる。
    """
    denials = envelope.get("permission_denials") or []
    turns = envelope.get("num_turns", 1)
    if denials or (isinstance(turns, int) and turns > 1):
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
        try:
            validated = schema.model_validate_json(raw_text)
        except ValidationError as exc:
            message = f"応答がスキーマを満たしません: {exc.error_count()} 件"
            raise LLMInvalidResponseError(message) from exc

        return LLMCompletion(
            data=validated.model_dump(),
            usage=_extract_usage(envelope),
            raw_text=raw_text,
        )

    def _invoke(self, prompt: str) -> dict[str, Any]:
        """CLI を実行して JSON 封筒を返す。"""
        command = _build_command(self._settings, prompt)
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 — 引数は自前で構築し shell を介さない
                command,
                capture_output=True,
                text=True,
                timeout=self._settings.llm_timeout_seconds,
                check=False,
                # 記事本文を扱うプロセスに標準入力を渡さない。
                stdin=subprocess.DEVNULL,
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
