"""LLM 呼び出しで発生するエラー。

失敗理由を構造化ログへ残せるよう `reason` を持たせる。
"""

from __future__ import annotations


class LLMError(Exception):
    """LLM 呼び出しの失敗を表す基底クラス。"""

    reason: str = "llm_failed"


class LLMTimeoutError(LLMError):
    """所定時間内に応答が返らなかった。"""

    reason = "llm_timeout"


class LLMInvocationError(LLMError):
    """CLI の起動または実行に失敗した。"""

    reason = "llm_invocation_failed"


class LLMInvalidResponseError(LLMError):
    """応答が期待する JSON スキーマを満たさなかった。"""

    reason = "llm_invalid_response"


class LLMToolUseDetectedError(LLMError):
    """ツールを無効化したはずの呼び出しでツール使用が観測された。

    記事本文は非信頼入力であり、ツールが動くこと自体が隔離の失敗を意味する。
    結果を採用せず、必ず失敗として扱う。
    """

    reason = "llm_tool_use_detected"


class LLMManagedPolicyDetectedError(LLMError):
    """実行ホストに管理者ポリシーが配布されている。

    ポリシー配下では CLI 側の隔離がほとんど機能しない（`llm.managed_policy` の
    説明を参照）。CLI を起動せずに失敗させる。再試行しても状況は変わらない。
    """

    reason = "llm_managed_policy_detected"
