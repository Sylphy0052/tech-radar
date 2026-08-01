"""LLM 呼び出しのリトライと構造化ログ記録。

一時的な失敗（タイムアウト・起動失敗・スキーマ不一致）は指数バックオフで再試行する。
ツール使用の検知だけは再試行しない。隔離の失敗であり、繰り返しても状況は変わらないため。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy.orm import Session

from techradar.config import Settings, get_settings
from techradar.db import OperationLog
from techradar.llm.base import LLMCompletion, LLMProvider
from techradar.llm.errors import LLMError, LLMToolUseDetectedError

# 再試行しても解消しない失敗。
NON_RETRYABLE = (LLMToolUseDetectedError,)


def complete_json_with_retry(
    provider: LLMProvider,
    *,
    instruction: str,
    untrusted_content: str,
    schema: type[BaseModel],
    operation: str,
    session: Session | None = None,
    article_id: object | None = None,
    settings: Settings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMCompletion:
    """リトライ付きで LLM を呼び出し、結果と失敗を構造化ログへ記録する。

    Args:
        operation: `operation_logs.operation` に記録する処理名。
        session: 記録先。None ならログを残さない（DB を使わないテスト向け）。
        sleep: バックオフの待機。テストから差し替える。

    Raises:
        LLMError: 全ての試行が失敗した場合、最後の例外を送出する。
    """
    resolved = settings or get_settings()
    attempts = resolved.llm_max_retries + 1
    started = time.monotonic()
    last_error: LLMError | None = None

    for attempt in range(attempts):
        try:
            completion = provider.complete_json(
                instruction=instruction,
                untrusted_content=untrusted_content,
                schema=schema,
            )
        except NON_RETRYABLE as exc:
            _record(
                session,
                operation=operation,
                status="failed",
                article_id=article_id,
                error_reason=exc.reason,
                details={"attempts": attempt + 1, "message": str(exc), "retryable": False},
            )
            raise
        except LLMError as exc:
            last_error = exc
            if attempt < attempts - 1:
                sleep(resolved.llm_retry_backoff_seconds * (2**attempt))
                continue
        else:
            _record(
                session,
                operation=operation,
                status="completed",
                article_id=article_id,
                model=completion.usage.model,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                duration_ms=completion.usage.duration_ms,
                details={"attempts": attempt + 1},
            )
            return completion

    duration_ms = int((time.monotonic() - started) * 1000)
    assert last_error is not None  # noqa: S101 — ループ構造上ここには失敗時のみ到達する
    _record(
        session,
        operation=operation,
        status="failed",
        article_id=article_id,
        duration_ms=duration_ms,
        error_reason=last_error.reason,
        details={"attempts": attempts, "message": str(last_error), "retryable": True},
    )
    raise last_error


def _record(
    session: Session | None,
    *,
    operation: str,
    status: str,
    article_id: object | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_ms: int | None = None,
    error_reason: str | None = None,
    details: dict | None = None,
) -> None:
    """`operation_logs` へ 1 件記録する。

    ログの失敗で本処理を巻き込まないよう、session が無い場合は何もしない。
    """
    if session is None:
        return
    session.add(
        OperationLog(
            operation=operation,
            status=status,
            article_id=article_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            error_reason=error_reason,
            details=details or {},
        )
    )
    session.flush()
