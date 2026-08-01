"""LLM プロバイダーの抽象。

MVP では Claude Code CLI を使うが、実装を差し替えられるようプロトコルで定義する
（`PROJECT_SPEC.md` §25「LLMプロバイダーを抽象化する」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMUsage:
    """1 回の呼び出しの使用量（`PROJECT_SPEC.md` §24 可観測性）。"""

    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


@dataclass(frozen=True)
class LLMCompletion:
    """LLM の応答。

    `data` はスキーマ検証済みの JSON。`usage` は構造化ログへ記録する。
    """

    data: dict[str, Any]
    usage: LLMUsage
    raw_text: str = field(repr=False, default="")


@runtime_checkable
class LLMProvider(Protocol):
    """LLM プロバイダーが満たすべきインターフェース。

    実装は「非信頼テキストを受け取り、検証済み JSON を返す」ことだけを担う。
    プロンプトの組み立ては `techradar.llm.prompt` が受け持つ。
    """

    name: str

    def complete_json(
        self,
        *,
        instruction: str,
        untrusted_content: str,
        schema: type,
    ) -> LLMCompletion:
        """指示と非信頼テキストから、`schema` で検証済みの JSON を得る。

        Args:
            instruction: 何を抽出するかの指示。信頼できる自前の文字列のみ。
            untrusted_content: 記事本文など外部由来のテキスト。
            schema: 応答を検証する Pydantic モデル。

        Raises:
            LLMError: 呼び出しまたは検証に失敗した場合。
        """
        ...
