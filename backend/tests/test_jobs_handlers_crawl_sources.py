"""`crawl_sources` ジョブハンドラを検証する結合テスト（Issue #9 T14）。

`process_crawl_sources` は `techradar.collectors.service.collect_candidates` を
呼ぶだけの薄い配線のため、実コレクターを差し替えて実 HTTP を避ける。
`source_domain` に IP リテラル・リンクローカルアドレスを渡した場合に
実際のリクエストが飛ばないことも確認する（MR !9 のレビュー申し送り、受入基準）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.collectors.base import CandidateArticle, SourceCollector
from techradar.collectors.service import CollectResult, collect_candidates
from techradar.config import Settings
from techradar.db.enums import JobType
from techradar.db.models import Job
from techradar.jobs.handlers import crawl_sources as crawl_sources_handler
from techradar.jobs.handlers.crawl_sources import process_crawl_sources
from techradar.jobs.registry import JobContext


class _FakeCollector:
    """テスト用の固定候補を返すコレクター。"""

    def __init__(self, name: str, candidates: Sequence[CandidateArticle] = ()) -> None:
        self.name = name
        self._candidates = tuple(candidates)

    def collect(self) -> Sequence[CandidateArticle]:
        return self._candidates


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_context(payload: dict[str, object] | None = None) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=JobType.CRAWL_SOURCES,
        payload=payload or {},
        attempts=0,
    )


def _patch_collect_candidates_with_collectors(
    monkeypatch: pytest.MonkeyPatch, collectors: Sequence[SourceCollector]
) -> None:
    """`collect_candidates` を、指定のフェイクコレクターだけで実行するよう差し替える。

    実コレクター（RSS/HN/GitHub Releases/arXiv/Brave）の構築自体を避けたいが、
    絞り込み・enqueue のロジックは本物のまま検証したいため、`collectors` 引数
    だけをすり替えた実装へ委譲する。
    """

    def fake(
        session: Session, *, settings: Settings | None = None, source_domain: str | None = None
    ) -> CollectResult:
        return collect_candidates(
            session, settings=settings, source_domain=source_domain, collectors=collectors
        )

    monkeypatch.setattr(crawl_sources_handler, "collect_candidates", fake)


class TestProcessCrawlSources:
    def test_enqueues_fetch_article_jobs_for_collected_candidates(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        candidate = CandidateArticle(
            url="https://example.com/articles/found",
            title="タイトル",
            published_at=datetime.now(UTC),
            collector_name="fake",
        )
        _patch_collect_candidates_with_collectors(
            monkeypatch, [_FakeCollector("fake", [candidate])]
        )

        # Act
        process_crawl_sources(db_session, make_context(), settings)

        # Assert
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1
        assert jobs[0].payload["url"] == "https://example.com/articles/found"

    def test_passes_the_source_domain_from_the_payload_through_to_the_collect_service(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        received: dict[str, object] = {}

        def fake_collect_candidates(session: Session, **kwargs: object) -> CollectResult:
            received.update(kwargs)
            return CollectResult(collected_count=0, excluded_count=0, enqueued_count=0)

        monkeypatch.setattr(crawl_sources_handler, "collect_candidates", fake_collect_candidates)

        # Act
        process_crawl_sources(db_session, make_context({"source_domain": "example.com"}), settings)

        # Assert
        assert received["source_domain"] == "example.com"

    def test_defaults_to_no_source_domain_when_the_payload_omits_it(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        received: dict[str, object] = {}

        def fake_collect_candidates(session: Session, **kwargs: object) -> CollectResult:
            received.update(kwargs)
            return CollectResult(collected_count=0, excluded_count=0, enqueued_count=0)

        monkeypatch.setattr(crawl_sources_handler, "collect_candidates", fake_collect_candidates)

        # Act
        process_crawl_sources(db_session, make_context(), settings)

        # Assert
        assert received["source_domain"] is None


class TestSourceDomainDoesNotReachTheNetwork:
    """受入基準: `source_domain` に危険な値を渡してもリクエストが飛ばない。

    `source_domain` は `collect_candidates` へ絞り込み条件として渡されるだけで、
    URL の組み立てには使われない（`process_crawl_sources` のコメント参照）。
    ここでは実コレクターを一切使わず（空リスト）、`source_domain` の値に関わらず
    ネットワークアクセスの起点自体が無いことを確認する。
    """

    @pytest.mark.parametrize(
        "dangerous_source_domain",
        [
            "169.254.169.254",
            "127.0.0.1",
            "[::1]",
            "metadata.internal",
        ],
    )
    def test_does_not_raise_for_ip_literal_or_link_local_source_domains(
        self,
        db_session: Session,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        dangerous_source_domain: str,
    ) -> None:
        # Arrange
        _patch_collect_candidates_with_collectors(monkeypatch, [])

        # Act / Assert — 例外を出さずに終了する
        process_crawl_sources(
            db_session, make_context({"source_domain": dangerous_source_domain}), settings
        )
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert jobs == []


class TestInvalidSourceDomainPayload:
    """`Job.payload` は JSONB のため文字列以外が入りうる。

    API を経ずに積まれたジョブでも、絞り込み側の文字列操作で
    `AttributeError` を起こさず「絞り込み指定なし」として続行する。
    """

    @pytest.mark.parametrize("invalid_source_domain", [123, ["example.com"], {"host": "a"}, True])
    def test_falls_back_to_no_scoping_when_source_domain_is_not_a_string(
        self,
        db_session: Session,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        invalid_source_domain: object,
    ) -> None:
        # Arrange
        candidate = CandidateArticle(
            url="https://example.com/a",
            title="A",
            published_at=datetime.now(UTC),
            collector_name="fake",
        )
        _patch_collect_candidates_with_collectors(
            monkeypatch, [_FakeCollector("fake", [candidate])]
        )

        # Act
        process_crawl_sources(
            db_session, make_context({"source_domain": invalid_source_domain}), settings
        )

        # Assert — 絞り込みなしとして扱われ、候補がそのまま enqueue される
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert [job.payload["url"] for job in jobs] == ["https://example.com/a"]


class TestPurgeJobIsEnqueuedAfterCrawling:
    """常駐スケジューラを置かない設計のため、ログ削除は巡回に便乗させる（Issue #19）。

    UI の実行ボタンが唯一の定期実行の契機であり、巡回が実際に走ったときにだけ
    `purge_operation_logs` を積むことで、保持期間 90 日の適用を実行主体のある
    処理にする。
    """

    def test_enqueues_a_purge_operation_logs_job_alongside_the_collected_candidates(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """候補が集まった通常の巡回でも、削除ジョブは 1 件だけ積まれる。"""
        # Arrange
        candidate = CandidateArticle(
            url="https://example.com/articles/found",
            title="タイトル",
            published_at=datetime.now(UTC),
            collector_name="fake",
        )
        _patch_collect_candidates_with_collectors(
            monkeypatch, [_FakeCollector("fake", [candidate])]
        )

        # Act
        process_crawl_sources(db_session, make_context(), settings)

        # Assert
        fetch_jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)
        ).all()
        purge_jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.PURGE_OPERATION_LOGS.value)
        ).all()
        assert len(fetch_jobs) == 1
        assert len(purge_jobs) == 1

    def test_enqueues_the_purge_job_even_when_no_candidate_is_collected(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """候補がゼロでもログの保持期間は経過するため、削除ジョブは積む。"""
        # Arrange
        _patch_collect_candidates_with_collectors(monkeypatch, [])

        # Act
        process_crawl_sources(db_session, make_context(), settings)

        # Assert
        fetch_jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)
        ).all()
        purge_jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.PURGE_OPERATION_LOGS.value)
        ).all()
        assert fetch_jobs == []
        assert len(purge_jobs) == 1


class TestRecommendationRunPurgeJobIsEnqueuedAfterCrawling:
    """`recommendation_runs` の削除も巡回に便乗させる（Issue #28）。

    `TestPurgeJobIsEnqueuedAfterCrawling`（Issue #19）と同じ理由（常駐
    スケジューラを置かない設計）で、巡回が実際に走ったときにだけ
    `purge_recommendation_runs` を積む。
    """

    def test_enqueues_a_purge_recommendation_runs_job_alongside_the_collected_candidates(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """候補が集まった通常の巡回でも、削除ジョブは 1 件だけ積まれる。"""
        # Arrange
        candidate = CandidateArticle(
            url="https://example.com/articles/found",
            title="タイトル",
            published_at=datetime.now(UTC),
            collector_name="fake",
        )
        _patch_collect_candidates_with_collectors(
            monkeypatch, [_FakeCollector("fake", [candidate])]
        )

        # Act
        process_crawl_sources(db_session, make_context(), settings)

        # Assert
        purge_jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.PURGE_RECOMMENDATION_RUNS.value)
        ).all()
        assert len(purge_jobs) == 1

    def test_enqueues_the_purge_job_even_when_no_candidate_is_collected(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """候補がゼロでも run の保持期間は経過するため、削除ジョブは積む。"""
        # Arrange
        _patch_collect_candidates_with_collectors(monkeypatch, [])

        # Act
        process_crawl_sources(db_session, make_context(), settings)

        # Assert
        purge_jobs = db_session.scalars(
            select(Job).where(Job.type == JobType.PURGE_RECOMMENDATION_RUNS.value)
        ).all()
        assert len(purge_jobs) == 1
