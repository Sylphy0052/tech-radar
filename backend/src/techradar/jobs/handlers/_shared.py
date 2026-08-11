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
from typing import Any

from sqlalchemy.exc import DBAPIError
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


def optional_registration_id(payload: dict[str, Any]) -> uuid.UUID | None:
    """payload から `registration_id` を取り出す。無ければ巡回由来として `None`。

    URL 登録（`ArticleRegistration` を伴う）と巡回由来（伴わない）の2経路が
    同じジョブ種別を共有するため、3 つのハンドラすべてがこの判定を必要とする。
    """
    raw = payload.get("registration_id")
    if raw is None:
        return None
    return uuid.UUID(raw)


def load_registration_if_present(
    session: Session, registration_id: uuid.UUID | None
) -> ArticleRegistration | None:
    """`registration_id` があれば登録行を取得する。無ければ `None`（巡回由来）。"""
    if registration_id is None:
        return None
    return load_registration(session, registration_id)


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


def record_registration_failure_safely(
    session: Session,
    registration_id: uuid.UUID,
    reason: RegistrationErrorReason,
    *,
    context: JobContext,
    settings: Settings,
) -> None:
    """処理が失敗したときに、トランザクションの状態によらず失敗理由を記録する。

    失敗の原因が DB 由来の例外（制約違反など）だと、その時点でトランザクションは
    中断状態になっている。そのまま `record_registration_failure` を呼ぶと書き込み自体が
    `InFailedSqlTransaction` で失敗し、記録が残らないばかりか呼び出し元へ伝播する例外まで
    元の原因からすり替わる。まず素直に記録を試し、書けなかったときだけ巻き戻して書き直す
    （中断しているかは `Session.is_active` では判別できない。DBAPI 側で中断していても
    セッションとしては active のままのため）。

    巻き戻すと、この試行が途中まで書いた内容（取得できた記事や実行中 status）も消える。
    ジョブは同じ payload で再試行されるため取り直せる一方、失敗理由を残せないままだと
    登録が実行中のまま取り残されるので、記録できることを優先する。

    この関数自体は決して例外を送出しない。呼び出し元は元の例外を再送出するために
    この呼び出しの後ろへ処理を続けるため、ここで別の例外を投げると原因がすり替わる。
    """
    first_error = _try_record_failure(
        session, registration_id, reason, context=context, settings=settings
    )
    if first_error is None:
        return

    try:
        session.rollback()
    except DBAPIError:
        # 接続そのものが切れている場合は巻き戻しすら通らない。記録は諦める
        # （ジョブ側の失敗記録は `jobs.last_error` に残る）。
        logger.exception(
            "job_handlers.failure_record_rollback_failed registration_id=%s job_id=%s",
            registration_id,
            context.job_id,
        )
        return

    retry_error = _try_record_failure(
        session, registration_id, reason, context=context, settings=settings
    )
    if retry_error is not None:
        # 記録を阻んだ例外そのものを残す。ここは呼び出し元の `except` の中であり、
        # 暗黙の例外情報は元の障害を指してしまうため、明示的に渡す。
        logger.error(
            "job_handlers.failure_record_failed registration_id=%s job_id=%s",
            registration_id,
            context.job_id,
            exc_info=retry_error,
        )


def _try_record_failure(
    session: Session,
    registration_id: uuid.UUID,
    reason: RegistrationErrorReason,
    *,
    context: JobContext,
    settings: Settings,
) -> DBAPIError | None:
    """失敗理由の記録を1度だけ試し、書けなかった場合はその例外を返す。

    握るのは DB とのやり取りに由来する例外だけにする。セッションの誤用など
    こちらの実装バグ由来の例外まで握ると、記録できない原因が「2回とも失敗」の
    ログ1行に丸められて追えなくなる。

    登録行が存在しない場合も「これ以上やることは無い」ため成功扱いにする
    （呼び出し側に巻き戻しと再試行をさせない）。
    """
    try:
        registration = session.get(ArticleRegistration, registration_id)
        if registration is None:
            logger.warning("job_handlers.registration_missing registration_id=%s", registration_id)
            return None
        record_registration_failure(
            session, registration, reason, context=context, settings=settings
        )
    except DBAPIError as exc:
        return exc
    return None


async def run_job_in_thread(
    context: JobContext, settings: Settings, operation: JobOperation
) -> None:
    """DB 操作を伴う同期処理を、イベントループを塞がない別スレッドで実行する。"""

    await asyncio.to_thread(_run_sync, context, settings, operation)


def _run_sync(context: JobContext, settings: Settings, operation: JobOperation) -> None:
    """`operation` をセッション付きで実行し、成否によらず結果を確定させる。

    失敗時も `operation` 内で `record_registration_failure_safely` により
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
