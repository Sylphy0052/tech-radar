"""`backend/scripts/requeue_failed_jobs.py` のテスト（Issue #79）。

2026-08-12にembed_articleジョブ194件が環境不備で全滅し、DBを直接UPDATEして
復旧した経緯を踏まえたスクリプト。以下を検証する。

- `_build_plan` の絞り込み（ジョブ種別・失敗理由の部分一致、completed/実行中の除外、
  `--error-contains`のLIKEワイルドカードエスケープ）
- `_check_max_requeue` のサーキットブレーカー
- `_apply_plan` の更新内容（attempts/last_error/started_at/finished_at/available_at の
  リセット、対象外ジョブを巻き込まないこと、`_build_plan`後に状態が変わった行を
  取りこぼすこと＝再確認によるレース対策）
- 対象が0件のときに何も起きないこと
- `main()` のCLI配線（`--apply`の有無と`--max-requeue`超過時にDBが実際に
  変更されないこと）
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from scripts import requeue_failed_jobs as cli
from scripts.requeue_failed_jobs import (
    DEFAULT_MAX_REQUEUE,
    TooManyRequeueTargetsError,
    _apply_plan,
    _build_plan,
    _check_max_requeue,
)
from sqlalchemy.orm import Session

from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Job
from techradar.jobs.queue import enqueue
from techradar.jobs.status import running_status_for


def _make_failed_job(
    session: Session,
    job_type: JobType,
    *,
    attempts: int = 3,
    last_error: str | None = "boom",
) -> Job:
    """`failed`状態のジョブを直接組み立てる。

    `fail()`をmax_attempts回呼んで到達させる遠回りを避け、テストで検証したい
    状態（failed・任意のattempts・任意のlast_error）を直接作る。
    """
    job = enqueue(session, job_type)
    job.status = JobStatus.FAILED.value
    job.attempts = attempts
    job.last_error = last_error
    job.started_at = datetime.now(UTC) - timedelta(minutes=5)
    job.finished_at = datetime.now(UTC)
    session.flush()
    return job


class TestBuildPlan:
    def test_finds_failed_jobs_of_the_given_type(self, db_session: Session) -> None:
        # Arrange
        target = _make_failed_job(db_session, JobType.EMBED_ARTICLE)
        _make_failed_job(db_session, JobType.FETCH_ARTICLE)

        # Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Assert
        assert [candidate.id for candidate in plan.candidates] == [target.id]

    def test_filters_by_error_substring(self, db_session: Session) -> None:
        # Arrange
        matching = _make_failed_job(
            db_session,
            JobType.EMBED_ARTICLE,
            last_error="sentence-transformersが利用できません",
        )
        _make_failed_job(db_session, JobType.EMBED_ARTICLE, last_error="別の理由で失敗")

        # Act
        plan = _build_plan(
            db_session, job_type=JobType.EMBED_ARTICLE, error_contains="sentence-transformers"
        )

        # Assert
        assert [candidate.id for candidate in plan.candidates] == [matching.id]

    def test_excludes_completed_jobs(self, db_session: Session) -> None:
        # Arrange
        completed = enqueue(db_session, JobType.EMBED_ARTICLE)
        completed.status = JobStatus.COMPLETED.value
        db_session.flush()

        # Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Assert
        assert plan.candidates == []

    def test_excludes_running_jobs(self, db_session: Session) -> None:
        # Arrange — embed_articleの実行中statusはanalyzing（jobs/status.py）
        running = enqueue(db_session, JobType.EMBED_ARTICLE)
        running.status = running_status_for(JobType.EMBED_ARTICLE).value
        running.started_at = datetime.now(UTC)
        db_session.flush()

        # Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Assert
        assert plan.candidates == []

    def test_returns_no_candidates_when_none_match(self, db_session: Session) -> None:
        # Arrange / Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Assert
        assert plan.candidates == []
        assert plan.requeued == []

    def test_error_contains_treats_underscore_as_literal(self, db_session: Session) -> None:
        """`--error-contains`のLIKEワイルドカードエスケープを固定する（Issue #79 self review）。

        Issue #79の復旧対象の失敗メッセージは`sentence-transformersが利用できません`
        （ハイフン）だが、対応するPythonのモジュール名は`sentence_transformers`
        （アンダースコア）である。運用者がどちらの表記で渡すかは状況次第で、`_`を
        エスケープしないとLIKEの「任意の1文字」として働き、`a_c`を渡したときに
        `abc`のような別文字列まで一致してしまう。
        """
        # Arrange
        literal_match = _make_failed_job(db_session, JobType.EMBED_ARTICLE, last_error="a_c")
        _make_failed_job(db_session, JobType.EMBED_ARTICLE, last_error="abc")

        # Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE, error_contains="a_c")

        # Assert — ワイルドカードとして解釈されず、"a_c"のみがリテラル一致する。
        assert [candidate.id for candidate in plan.candidates] == [literal_match.id]

    def test_error_contains_treats_percent_as_literal(self, db_session: Session) -> None:
        """`%`もLIKEのワイルドカード（任意の0文字以上）として解釈されないことを固定する。"""
        # Arrange
        literal_match = _make_failed_job(db_session, JobType.EMBED_ARTICLE, last_error="100%失敗")
        _make_failed_job(db_session, JobType.EMBED_ARTICLE, last_error="100失敗")

        # Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE, error_contains="100%失敗")

        # Assert
        assert [candidate.id for candidate in plan.candidates] == [literal_match.id]

    def test_error_contains_treats_a_backslash_as_literal(self, db_session: Session) -> None:
        r"""エスケープ文字自体（`\`）もリテラルとして扱われることを固定する（Issue #97）。

        PostgreSQLのLIKEは`ESCAPE`句が無くても`\`をエスケープ文字として扱うため、
        二重化しないまま`C:\temp`を渡すと`\t`が`t`と解釈され、バックスラッシュを含まない
        `C:temp`まで一致してしまう。Windowsのパスやスタックトレースを失敗メッセージから
        コピーして渡す運用では現実に踏みうる。

        既存の`%`・`_`の2件では、この経路は押さえられていない。2026-08-15に実測した
        （置換の記述はIssue #97の時点のもの）。

        - 二重化の行だけを外す → このテストのみが落ち、`%`・`_`の2件は通る
        - `%`・`_`を先に処理する順序へ入れ替える → `%`・`_`の2件が落ち、このテストは通る

        つまり置換順序は既存の2件が既に押さえており、このテストが埋めるのは
        「エスケープ文字自体を二重化すること」の側である。

        `db/query.py`の単体テスト（`test_db_query.py`）は文字列変換の結果だけを見ており、
        実際にPostgreSQLのLIKEを通した挙動までは見ていない。ここで押さえる。
        """
        # Arrange
        literal_match = _make_failed_job(
            db_session, JobType.EMBED_ARTICLE, last_error="failed at C:\\temp"
        )
        _make_failed_job(db_session, JobType.EMBED_ARTICLE, last_error="failed at C:temp")

        # Act
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE, error_contains="C:\\temp")

        # Assert
        assert [candidate.id for candidate in plan.candidates] == [literal_match.id]


class TestCheckMaxRequeue:
    def test_raises_when_candidates_exceed_the_limit(self, db_session: Session) -> None:
        # Arrange
        for _ in range(3):
            _make_failed_job(db_session, JobType.EMBED_ARTICLE)
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Act / Assert
        with pytest.raises(TooManyRequeueTargetsError) as exc_info:
            _check_max_requeue(plan, max_requeue=2)
        assert "3件" in str(exc_info.value)
        assert "--max-requeue 2" in str(exc_info.value)

    def test_does_not_raise_when_within_the_limit(self, db_session: Session) -> None:
        # Arrange
        _make_failed_job(db_session, JobType.EMBED_ARTICLE)
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Act / Assert — 例外が出なければ成功
        _check_max_requeue(plan, max_requeue=DEFAULT_MAX_REQUEUE)

    def test_zero_means_unlimited(self, db_session: Session) -> None:
        # Arrange
        for _ in range(5):
            _make_failed_job(db_session, JobType.EMBED_ARTICLE)
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Act / Assert — 例外が出なければ成功
        _check_max_requeue(plan, max_requeue=0)


class TestApplyPlan:
    def test_resets_the_job_to_a_retryable_pending_state(self, db_session: Session) -> None:
        # Arrange
        job = _make_failed_job(db_session, JobType.EMBED_ARTICLE, attempts=5, last_error="環境不備")
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Act
        result = _apply_plan(db_session, plan)

        # Assert
        db_session.refresh(job)
        assert job.status == JobStatus.PENDING.value
        assert job.attempts == 0
        assert job.last_error is None
        assert job.started_at is None
        assert job.finished_at is None
        assert job.available_at <= datetime.now(UTC)
        assert [candidate.id for candidate in result.requeued] == [job.id]

    def test_does_not_touch_jobs_outside_the_plan(self, db_session: Session) -> None:
        # Arrange — 対象外（別種別）のジョブが巻き込まれないこと
        target = _make_failed_job(db_session, JobType.EMBED_ARTICLE)
        other = _make_failed_job(db_session, JobType.FETCH_ARTICLE, attempts=2)
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Act
        _apply_plan(db_session, plan)

        # Assert
        db_session.refresh(target)
        db_session.refresh(other)
        assert target.status == JobStatus.PENDING.value
        assert other.status == JobStatus.FAILED.value
        assert other.attempts == 2

    def test_does_nothing_when_there_are_no_candidates(self, db_session: Session) -> None:
        # Arrange
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)

        # Act
        result = _apply_plan(db_session, plan)

        # Assert
        assert result.requeued == []
        assert result.candidates == []

    def test_skips_a_job_whose_status_changed_after_the_plan_was_built(
        self, db_session: Session
    ) -> None:
        """`_build_plan`から`_apply_plan`までの間に対象行のstatusが変わっていた場合、
        UPDATE文のWHERE句（status == failed）に引っかからず自然に対象外になることを
        確認する（Issue #79のリスク注記に対応するレース対策）。
        """
        # Arrange
        job = _make_failed_job(db_session, JobType.EMBED_ARTICLE)
        plan = _build_plan(db_session, job_type=JobType.EMBED_ARTICLE)
        # 別の操作が先にpendingへ戻した状況を模倣する。
        job.status = JobStatus.PENDING.value
        job.available_at = datetime.now(UTC) - timedelta(minutes=1)
        db_session.flush()

        # Act
        result = _apply_plan(db_session, plan)

        # Assert — WHERE句の再確認で対象外になり、requeuedに含まれない。
        assert result.requeued == []
        db_session.refresh(job)
        assert job.status == JobStatus.PENDING.value


@contextmanager
def _yield_existing_session(session: Session) -> Iterator[Session]:
    """`main()`内の`session_scope()`の代わりに、テストの`db_session`をそのまま渡す。

    `db_session`フィクスチャは外側のトランザクションを張ってテストごとにロールバック
    する。`main()`が本物の`session_scope()`を使うと別コネクションを開いてしまい、
    このフィクスチャでまだコミットしていない変更が見えない。そのため`main()`が
    開くセッションを同じ`db_session`に差し替え、commit/closeは呼び出し元
    （`db_session`フィクスチャ）に委ねる。
    """
    yield session


@pytest.fixture
def use_db_session_in_main(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Session:
    """`main()`内の`session_scope()`を、テストの`db_session`を返すものに差し替える。"""
    monkeypatch.setattr(cli, "session_scope", lambda: _yield_existing_session(db_session))
    return db_session


class TestMain:
    """`main()`のCLI配線を検証する（Issue #79 self review MEDIUM指摘）。

    `_build_plan`/`_check_max_requeue`/`_apply_plan`の単体テストだけでは、実際に
    叩かれる`main()`の配線（`--apply`の有無、上限超過時に本当に何も更新しないか）を
    検証できていなかった。破壊的スクリプトの「安全側に倒れる」という要求は、内部関数
    ではなく実際に叩かれる経路で担保する。
    """

    def test_without_apply_does_not_modify_the_database(
        self, use_db_session_in_main: Session, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        job = _make_failed_job(use_db_session_in_main, JobType.EMBED_ARTICLE)

        # Act
        exit_code = cli.main(["--type", "embed_article"])

        # Assert
        assert exit_code == 0
        use_db_session_in_main.refresh(job)
        assert job.status == JobStatus.FAILED.value
        assert "dry-run" in capsys.readouterr().out

    def test_apply_over_the_limit_returns_1_and_does_not_modify_the_database(
        self, use_db_session_in_main: Session, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        jobs = [_make_failed_job(use_db_session_in_main, JobType.EMBED_ARTICLE) for _ in range(2)]

        # Act
        exit_code = cli.main(["--type", "embed_article", "--apply", "--max-requeue", "1"])

        # Assert — 上限超過時は`_apply_plan`が呼ばれず、何も更新されない。
        assert exit_code == 1
        for job in jobs:
            use_db_session_in_main.refresh(job)
            assert job.status == JobStatus.FAILED.value
        assert "[requeue-failed-jobs][ERROR]" in capsys.readouterr().err

    def test_apply_within_the_limit_requeues_the_job(self, use_db_session_in_main: Session) -> None:
        # Arrange
        job = _make_failed_job(use_db_session_in_main, JobType.EMBED_ARTICLE)

        # Act
        exit_code = cli.main(["--type", "embed_article", "--apply"])

        # Assert
        assert exit_code == 0
        use_db_session_in_main.refresh(job)
        assert job.status == JobStatus.PENDING.value

    def test_max_requeue_zero_warns_about_unlimited(
        self, use_db_session_in_main: Session, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Arrange
        _make_failed_job(use_db_session_in_main, JobType.EMBED_ARTICLE)

        # Act
        exit_code = cli.main(["--type", "embed_article", "--max-requeue", "0"])

        # Assert — 修正3: 0＝上限なしを維持する代わりに、実行時に明示する。
        assert exit_code == 0
        assert "上限なし" in capsys.readouterr().out
