"""失敗したジョブを`pending`へ戻すスクリプト（Issue #79）。

2026-08-12、`embed_article` ジョブ194件が環境不備（venvの不完全インストール）で
全滅し、DBを直接UPDATEして復旧した。原因は解消済みでも、失敗したジョブを再実行する
導線が無かった（`jobs/queue.py`の`fail()`は`attempts >= max_attempts`で`failed`へ落とし、
以降は自動で再試行しない設計）。

このスクリプトはその復旧手順を汎用化したもの。API（`api/jobs.py`）ではなくスクリプトに
した理由は次の3点（判断の詳細はIssue #79のコメントにも残す）。

1. 今回の復旧は「人が状況を見て判断し、手で実行する」運用作業であり、UIから常時
   叩ける機能ではない。Issue #79は「失敗ジョブのUI表示」自体を対象外にしており、
   UIから呼ぶ導線を作る計画も無い。UIの無いAPIエンドポイントは、結局curl等で
   叩く以外に使われず、スクリプトと比べて得るものが無い。
2. `api/jobs.py`の`GET /api/jobs/{job_id}`は無認証で叩ける設計（同ファイルの
   `JobResponse`のdocstring参照）。単発の参照系エンドポイントと違い、この機能は
   ジョブ種別・失敗理由で絞り込んだ複数件を一括で`pending`へ戻す破壊的操作であり、
   認証の無いAPIにこれを追加で公開するのはリスクを増やすだけで割に合わない。
3. `scripts/cleanup-test-databases.sh`が「引数なしでdry-run、`--apply`で実行、
   件数上限で止める」という形を既にこのリポジトリの運用スクリプトの流儀にしている。
   同じ形に揃えることで、レビューする側もこのスクリプトの安全性を同じ観点で確認できる。

既定はdry-run（対象件数の表示のみ）。実際に戻すには`--apply`を渡す。

    cd backend && uv run python -m scripts.requeue_failed_jobs --type embed_article
    cd backend && uv run python -m scripts.requeue_failed_jobs --type embed_article --apply
    cd backend && uv run python -m scripts.requeue_failed_jobs \\
        --type embed_article --error-contains "sentence-transformers" --apply

リポジトリルートからは薄いラッパー`scripts/requeue-failed-jobs.sh`を使う。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from techradar.db import session_scope
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job

# dry-runのレポート表示でlast_errorを短く保つための切り詰め長。`jobs/queue.py`の
# MAX_LAST_ERROR_LENGTH（DB保存上限）とは目的が別のため、値を共有せず独立して持つ。
_REPORT_ERROR_PREVIEW_LENGTH = 200

# --max-requeueの既定値。`cleanup_test_databases.DEFAULT_MAX_DELETE`（10件）と同じ
# 「サーキットブレーカー」の考え方だが、ジョブの障害は一度に数百件溜まりうる
# （今回の契機はembed_article 194件）。既定値は小さく保ち、実際の復旧では
# 明示的に--max-requeueを引き上げて「意図した大量操作である」ことを示す運用にする。
DEFAULT_MAX_REQUEUE = 50

# `--error-contains`をLIKE（`Column.contains`）へ渡す際のエスケープ文字。
# LIKEでは`%`（任意の0文字以上）と`_`（任意の1文字）がワイルドカードとして働く。
# 運用者が失敗メッセージの一部をコピーして渡す前提のスクリプトだが、失敗メッセージと
# Pythonのモジュール名が表記として一致しない場合がある。実際にIssue #79の復旧対象の
# 失敗メッセージは「sentence-transformersが利用できません」（ハイフン）だが、
# 対応するPythonのモジュール名は`sentence_transformers`（アンダースコア）である。
# 運用者がどちらの表記で渡すかは状況次第で、`_`を含む文字列をエスケープせずに渡すと
# ワイルドカードとして働き、意図した以外の行まで一括UPDATEの対象に入ってしまう。
# そのため`%`・`_`・エスケープ文字自体をエスケープしたうえで、`contains(escape=...)`へ
# 明示的なエスケープ文字を渡す。
_LIKE_ESCAPE_CHAR = "\\"


def _escape_like_wildcards(value: str) -> str:
    """LIKE演算子のワイルドカード（`%`・`_`）をリテラル文字として扱うためにエスケープする。

    エスケープ文字自体（`\\`）が値に含まれている場合、それを先にエスケープしておかないと
    後段の置換で二重にエスケープされてしまうため、最初に処理する。
    """
    return (
        value.replace(_LIKE_ESCAPE_CHAR, _LIKE_ESCAPE_CHAR * 2)
        .replace("%", _LIKE_ESCAPE_CHAR + "%")
        .replace("_", _LIKE_ESCAPE_CHAR + "_")
    )


class TooManyRequeueTargetsError(RuntimeError):
    """`--apply`時の対象件数が`--max-requeue`を超えたことを表す。

    想定外の大量操作を機械的に止めるためのサーキットブレーカー
    （`cleanup_test_databases.TooManyDeletionsError`と同じ考え方）。この例外を
    受けた呼び出し側（`main`）は、何も更新せずに終了する。
    """


@dataclass(frozen=True)
class FailedJobSummary:
    """dry-runのレポートと適用結果の両方で使う、ジョブ1件分の要約。"""

    id: uuid.UUID
    type: str
    attempts: int
    last_error: str | None
    created_at: datetime


@dataclass(frozen=True)
class RequeuePlan:
    """1回の実行で何を戻したか／戻すかをまとめたレポート。"""

    job_type: JobType
    error_contains: str | None
    candidates: list[FailedJobSummary]
    # dry-runでは空のまま。`_apply_plan`が実際に更新できた行だけを入れる。
    requeued: list[FailedJobSummary]


def _summarize(job: Job) -> FailedJobSummary:
    return FailedJobSummary(
        id=job.id,
        type=job.type,
        attempts=job.attempts,
        last_error=job.last_error,
        created_at=job.created_at,
    )


def _build_plan(
    session: Session, *, job_type: JobType, error_contains: str | None = None
) -> RequeuePlan:
    """`failed`のジョブから、指定条件に合う対象を洗い出す。

    `status == JobStatus.FAILED.value`だけを条件にすることで、実行中
    （`jobs/status.py`の`RUNNING_STATUSES` — fetching/analyzing/searching）と
    `completed`のジョブは構造的に対象へ入らない。`claim_next`（`jobs/queue.py`）は
    `pending`のジョブしか実行中へ遷移させないため、`failed`のジョブが横から
    実行中へ変わる経路はワーカー側に存在しない。

    `error_contains`はLIKEによる部分一致（`Column.contains`）で、`%`・`_`は
    `_escape_like_wildcards`でエスケープしたうえでリテラルとして扱う（理由は
    `_LIKE_ESCAPE_CHAR`のコメント参照）。
    """
    stmt = select(Job).where(Job.status == JobStatus.FAILED.value, Job.type == job_type.value)
    if error_contains:
        stmt = stmt.where(
            Job.last_error.contains(
                _escape_like_wildcards(error_contains), escape=_LIKE_ESCAPE_CHAR
            )
        )
    stmt = stmt.order_by(Job.created_at)
    jobs = session.scalars(stmt).all()
    return RequeuePlan(
        job_type=job_type,
        error_contains=error_contains,
        candidates=[_summarize(job) for job in jobs],
        requeued=[],
    )


def _check_max_requeue(plan: RequeuePlan, max_requeue: int) -> None:
    """`--apply`時のサーキットブレーカー。超えていたら`TooManyRequeueTargetsError`を送出する。

    `max_requeue`に0以下を指定すると上限なしとして扱う
    （`cleanup_test_databases._check_max_delete`と同じ）。
    """
    if max_requeue <= 0:
        return
    if len(plan.candidates) <= max_requeue:
        return
    message = (
        f"対象が{len(plan.candidates)}件あり、上限（--max-requeue {max_requeue}件）を"
        "超えています。想定外に多い可能性があります。dry-run"
        "（--applyを付けずに実行）で内容を確認し、意図どおりであれば"
        "--max-requeue Nを指定して再実行してください。"
    )
    raise TooManyRequeueTargetsError(message)


def _apply_plan(session: Session, plan: RequeuePlan) -> RequeuePlan:
    """`plan.candidates`を実際に`pending`へ戻す。

    UPDATE文のWHERE句に`status == JobStatus.FAILED.value`を再度含める。
    `_build_plan`から呼び出しまでの間に対象行の状態が変わっていた場合
    （別セッションがこのスクリプトを同時に実行した等）、その行だけが自然に
    対象から外れる（他の行の更新は妨げない）。`failed`のジョブが横から実行中へ
    変わる経路はワーカー側に無い（`_build_plan`のdocstring参照）ため、実務上は
    このリポジトリでこのスクリプトを二重に実行した場合だけがこの再確認の対象になる。

    リセットする列と理由:

    - `status`: `pending`に戻す（再実行の起点）。
    - `attempts`: 0にリセットする。Issue #79の手動復旧時のSQLと同じ扱いで、
      環境側の不備など「ジョブ側に非のない失敗」を戻す想定のため、使い切った
      リトライ回数を持ち越さない。
    - `last_error`: NULLにする。`complete()`が成功時にNULLへ戻すのと同じ扱いで、
      「エラーの残っていない状態から再実行する」という意味を揃える。dry-runの
      レポートでは更新前の`last_error`をそのまま表示するため、絞り込みの根拠
      （どのエラーで戻したか）はレポート側にだけ残る。
    - `started_at`: Noneにする。`release()`が中断時に行う扱いと同じ
      （claimされていない状態に戻す）。
    - `finished_at`: Noneにする。終了していないジョブの状態に揃える。
    - `available_at`: 現在時刻にする。リトライの指数バックオフ（`fail()`参照）で
      将来の時刻が残っている可能性を考慮せず、常に「今すぐ実行可能」にする。
      今回の復旧の目的（原因解消後にすぐ再実行したい）に合わせるため。
    """
    ids = [candidate.id for candidate in plan.candidates]
    if not ids:
        return plan

    now = datetime.now(UTC)
    stmt = (
        update(Job)
        .where(Job.status == JobStatus.FAILED.value, Job.id.in_(ids))
        .values(
            status=JobStatus.PENDING.value,
            attempts=0,
            last_error=None,
            started_at=None,
            finished_at=None,
            available_at=now,
        )
        .returning(Job.id)
    )
    result = session.execute(stmt)
    requeued_ids = {row[0] for row in result}
    session.flush()

    requeued = [candidate for candidate in plan.candidates if candidate.id in requeued_ids]
    return RequeuePlan(
        job_type=plan.job_type,
        error_contains=plan.error_contains,
        candidates=plan.candidates,
        requeued=requeued,
    )


def _format_error_preview(last_error: str | None) -> str:
    if last_error is None:
        return "(なし)"
    if len(last_error) <= _REPORT_ERROR_PREVIEW_LENGTH:
        return last_error
    return last_error[:_REPORT_ERROR_PREVIEW_LENGTH] + "…"


def _print_report(plan: RequeuePlan, *, applied: bool, max_requeue: int) -> None:
    """人が読める形で対象・結果を報告する。"""
    filter_desc = f"type={plan.job_type.value}"
    if plan.error_contains:
        filter_desc += f", error_contains={plan.error_contains!r}"
    print(f"[requeue-failed-jobs] 絞り込み条件: {filter_desc}")
    print(f"[requeue-failed-jobs] 対象（failedかつ条件に一致）: {len(plan.candidates)}件")

    if max_requeue <= 0:
        # 0は「上限なし」を意味する（`_check_max_requeue`参照）。破壊的操作の
        # サーキットブレーカーが働かない状態であることに気付けるよう、dry-run・
        # --apply実行時のどちらでも明示する。
        print(
            "[requeue-failed-jobs][WARN] --max-requeueが0のため、件数の上限なし"
            "（サーキットブレーカーが働かない状態）で実行します。"
        )

    if not plan.candidates:
        print("[requeue-failed-jobs] 対象なし")
        return

    for candidate in plan.candidates:
        print(
            f"  - {candidate.id} attempts={candidate.attempts} "
            f"created_at={candidate.created_at.isoformat()} "
            f"last_error={_format_error_preview(candidate.last_error)}"
        )

    if applied:
        print(f"[requeue-failed-jobs] pendingへ戻しました: {len(plan.requeued)}件")
    else:
        print(
            "[requeue-failed-jobs] dry-runのため実際には戻していません。"
            "--applyを指定すると上記をpendingへ戻します。"
        )
        if max_requeue > 0 and len(plan.candidates) > max_requeue:
            print(
                "[requeue-failed-jobs][WARN] 対象が"
                f"{len(plan.candidates)}件あり、--max-requeue（{max_requeue}件）を"
                "超えています。--apply時はこのままでは中止されます。内容を確認し、"
                "意図どおりであれば--max-requeue Nを指定してください。"
            )


def main(argv: list[str] | None = None) -> int:
    """`failed`のジョブを`pending`へ戻す。`--apply`指定時のみ実際に更新する。"""
    parser = argparse.ArgumentParser(
        description="失敗したジョブをpendingへ戻す（既定はdry-run）。",
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=[job_type.value for job_type in JobType],
        help="対象のジョブ種別。",
    )
    parser.add_argument(
        "--error-contains",
        default=None,
        help="last_errorにこの文字列を含むジョブだけに絞り込む（部分一致）。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="実際にpendingへ戻す。指定しない場合はdry-run（対象の表示のみ）。",
    )
    parser.add_argument(
        "--max-requeue",
        type=int,
        default=DEFAULT_MAX_REQUEUE,
        help=(
            "--apply時に一度に戻してよい件数の上限（サーキットブレーカー、既定"
            f"{DEFAULT_MAX_REQUEUE}件）。超えた場合は何も更新せずエラー終了する。"
            "0を指定すると上限なし。"
        ),
    )
    args = parser.parse_args(argv)
    if args.max_requeue < 0:
        parser.error("--max-requeueは0以上を指定してください。")

    job_type = JobType(args.type)

    with session_scope() as session:
        plan = _build_plan(session, job_type=job_type, error_contains=args.error_contains)
        if args.apply:
            try:
                _check_max_requeue(plan, args.max_requeue)
            except TooManyRequeueTargetsError as exc:
                print(f"[requeue-failed-jobs][ERROR] {exc}", file=sys.stderr)
                return 1
            plan = _apply_plan(session, plan)
        _print_report(plan, applied=args.apply, max_requeue=args.max_requeue)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
