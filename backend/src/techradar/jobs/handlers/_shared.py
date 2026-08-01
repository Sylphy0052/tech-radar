"""3 つのハンドラ（fetch_article / analyze_article / embed_article）で共通の処理。

`JobContext` のコメントにある通り、ハンドラは `asyncio.to_thread` の外側
（イベントループ側）から呼ばれる。そのため各ハンドラは自分の中で同期処理を
スレッドへ逃がす必要がある（`run_job_in_thread`）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session

from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import ArticleRegistration
from techradar.db.session import get_session_factory
from techradar.jobs.handlers.errors import RegistrationErrorReason
from techradar.jobs.registry import JobContext
from techradar.jobs.status import running_status_for

logger = logging.getLogger(__name__)

# ハンドラが受け取る「1件のジョブを処理する」同期処理。
JobOperation = Callable[[Session, JobContext, Settings], None]


def is_final_attempt(context: JobContext, settings: Settings) -> bool:
    """このハンドラ呼び出しが、ジョブとして最後の試行になるかを判定する。

    `techradar.jobs.queue.fail` は呼び出しのたびに `attempts` を 1 増やして
    から `max_attempts` と比較する。ハンドラ側からは増える前の値
    （`context.attempts`）しか見えないため、+1 したうえで同じ条件で比較する。
    ここでの判定がずれると、まだリトライされるはずの登録を早まって
    `failed` にしてしまう。
    """
    return context.attempts + 1 >= settings.job_max_attempts


def load_registration(session: Session, registration_id: uuid.UUID) -> ArticleRegistration | None:
    """登録行を取得する。

    登録行が既に削除されている場合は `None` を返す。呼び出し側は
    「処理対象が無い」として早期に終了してよい（リトライしても解決しないため）。
    """
    registration = session.get(ArticleRegistration, registration_id)
    if registration is None:
        logger.warning("job_handlers.registration_missing registration_id=%s", registration_id)
    return registration


def start_registration_step(
    session: Session, registration: ArticleRegistration, job_type: JobType
) -> None:
    """この段階の処理を開始したことを登録行へ反映する。

    直前の試行が残した `error_reason` はここで消す。リトライ枠が残っている間の
    失敗も理由を書き込むため、消さずに進むと再試行が成功しても古い理由が残り、
    `status=completed` と失敗理由が同時に返る矛盾したレスポンスになる。
    """
    registration.status = running_status_for(job_type).value
    registration.error_reason = None
    session.flush()


def record_registration_failure(
    session: Session,
    registration: ArticleRegistration,
    reason: RegistrationErrorReason,
    *,
    context: JobContext,
    settings: Settings,
) -> None:
    """失敗理由を登録行へ記録する。

    リトライ枠がまだ残っている場合は `status` を変えない。実行中 status の
    ままにしておくことで、UI 側は「まだリトライ中」と「リトライを使い切って
    恒久的に失敗した」を区別できる（後者だけ `failed` にする）。
    """
    registration.error_reason = reason.value
    if is_final_attempt(context, settings):
        registration.status = JobStatus.FAILED.value
    session.flush()


async def run_job_in_thread(
    context: JobContext, settings: Settings, operation: JobOperation
) -> None:
    """DB 操作を伴う同期処理を、イベントループを塞がない別スレッドで実行する。"""

    await asyncio.to_thread(_run_sync, context, settings, operation)


def _run_sync(context: JobContext, settings: Settings, operation: JobOperation) -> None:
    """`operation` をセッション付きで実行し、成否によらず結果を確定させる。

    失敗時も `operation` 内で `record_registration_failure` により
    分類済みの理由が既に flush 済みのため、ここで rollback するとその記録
    ごと失ってしまう。そのため例外発生時も commit してから再送出する
    （ワーカー側の `fail()` はジョブ自体の状態遷移を別途行う）。
    """
    session = get_session_factory()()
    try:
        operation(session, context, settings)
        session.commit()
    except Exception:
        try:
            session.commit()
        except Exception:
            logger.exception(
                "job_handlers.commit_after_failure_also_failed job_id=%s", context.job_id
            )
            session.rollback()
        raise
    finally:
        session.close()
