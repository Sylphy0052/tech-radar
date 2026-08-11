"""LLM 層。

記事本文は非信頼入力として扱う。プロンプト構築・ツール無効化・スキーマ検証を
このパッケージへ隔離し、呼び出し側は検証済みの JSON だけを受け取る。
"""

from techradar.llm.base import LLMCompletion, LLMProvider, LLMUsage
from techradar.llm.claude_cli import DENIED_TOOLS, ClaudeCliProvider
from techradar.llm.errors import (
    LLMError,
    LLMInvalidResponseError,
    LLMInvocationError,
    LLMManagedPolicyDetectedError,
    LLMTimeoutError,
    LLMToolUseDetectedError,
)
from techradar.llm.fake import FakeLLMProvider
from techradar.llm.managed_policy import assert_no_managed_policy
from techradar.llm.prompt import (
    SYSTEM_PROMPT,
    UNTRUSTED_CLOSE_TAG,
    UNTRUSTED_OPEN_TAG,
    build_user_prompt,
    neutralize_delimiters,
)
from techradar.llm.retry import complete_json_with_retry

__all__ = [
    "DENIED_TOOLS",
    "SYSTEM_PROMPT",
    "UNTRUSTED_CLOSE_TAG",
    "UNTRUSTED_OPEN_TAG",
    "ClaudeCliProvider",
    "FakeLLMProvider",
    "LLMCompletion",
    "LLMError",
    "LLMInvalidResponseError",
    "LLMInvocationError",
    "LLMManagedPolicyDetectedError",
    "LLMProvider",
    "LLMTimeoutError",
    "LLMToolUseDetectedError",
    "LLMUsage",
    "assert_no_managed_policy",
    "build_user_prompt",
    "complete_json_with_retry",
    "neutralize_delimiters",
]
