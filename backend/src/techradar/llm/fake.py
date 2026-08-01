"""テストと開発で使う `LLMProvider` の代替実装。

`LLMProvider` が差し替え可能な抽象になっていることを実際に示す役割も持つ
（`PROJECT_SPEC.md` §25「LLMプロバイダーを抽象化する」）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic import BaseModel, ValidationError

from techradar.llm.base import LLMCompletion, LLMUsage
from techradar.llm.errors import LLMError, LLMInvalidResponseError


class FakeLLMProvider:
    """あらかじめ用意した応答を順に返すプロバイダー。

    応答の代わりに例外を並べると、その回の呼び出しでその例外を送出する。
    リトライの挙動を検証するために使う。
    """

    name = "fake"

    def __init__(self, responses: Sequence[str | LLMError | dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def complete_json(
        self,
        *,
        instruction: str,
        untrusted_content: str,
        schema: type[BaseModel],
    ) -> LLMCompletion:
        """次の応答を返す。応答が尽きたら最後のものを繰り返す。"""
        self.calls.append({"instruction": instruction, "untrusted_content": untrusted_content})

        index = min(len(self.calls) - 1, len(self._responses) - 1)
        response = self._responses[index]

        if isinstance(response, LLMError):
            raise response

        raw_text = (
            json.dumps(response, ensure_ascii=False) if isinstance(response, dict) else response
        )
        try:
            validated = schema.model_validate_json(raw_text)
        except ValidationError as exc:
            message = f"応答がスキーマを満たしません: {exc.error_count()} 件"
            raise LLMInvalidResponseError(message) from exc

        return LLMCompletion(
            data=validated.model_dump(),
            usage=LLMUsage(model="fake-model", input_tokens=100, output_tokens=20, duration_ms=5),
            raw_text=raw_text,
        )
