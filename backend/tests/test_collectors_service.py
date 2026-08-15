"""`techradar.collectors.service.collect_candidates` の振る舞いテスト（Issue #9 T13）。

実 HTTP へは一切出ない。デフォルトのコレクター構築ではなく、フェイクの
`SourceCollector` を `collectors` 引数で明示的に渡すことで、収集ロジック
以降の絞り込み・enqueue の振る舞いだけを検証する。`_build_default_collectors`
自体を検証するテストのみ、コレクタークラスを module レベルで差し替える。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import techradar.collectors.service as service
from techradar.collectors.base import CandidateArticle, CollectorError
from techradar.collectors.brave import BraveSearchCollector
from techradar.collectors.config import FeedsConfig
from techradar.collectors.rss import FeedFetchResult
from techradar.collectors.service import CRAWL_ORIGIN, collect_candidates
from techradar.config import Settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article, Job
from techradar.fetcher.url import normalize_url

NOW = datetime.now(UTC)


class _FakeCollector:
    """テスト用の固定候補を返す（または例外を送出する）コレクター。"""

    def __init__(
        self,
        name: str,
        candidates: Sequence[CandidateArticle] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._candidates = tuple(candidates)
        self._error = error

    def collect(self) -> Sequence[CandidateArticle]:
        if self._error is not None:
            raise self._error
        return self._candidates


def _stub_collector_factory(name: str, built_names: list[str]):
    """`_build_default_collectors` が呼ぶコンストラクタの差し替え用ファクトリを返す。

    実コレクター（RSS/HN/GitHub Releases/arXiv）は構築時に外部通信をしないが、
    ここでは構築されたこと自体を記録するだけのフェイクに差し替え、`collect()`
    が万一呼ばれても実 HTTP に出ないようにする。
    """

    def factory(*_args: object, **_kwargs: object) -> _FakeCollector:
        built_names.append(name)
        return _FakeCollector(name)

    return factory


def make_candidate(
    url: str,
    *,
    title: str = "タイトル",
    published_at: datetime | None = NOW,
    collector_name: str = "fake",
) -> CandidateArticle:
    return CandidateArticle(
        url=url,
        title=title,
        published_at=published_at,
        collector_name=collector_name,
    )


def make_feeds_config(*, freshness_days: int = 7, max_candidates_per_run: int = 100) -> FeedsConfig:
    return FeedsConfig(
        freshness_days=freshness_days,
        max_candidates_per_run=max_candidates_per_run,
        hacker_news_top_items=10,
    )


def make_article(session: Session, url: str) -> Article:
    article = Article(
        canonical_url=normalize_url(url),
        original_url=url,
        title="既存記事",
        source_domain="example.com",
    )
    session.add(article)
    session.flush()
    return article


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class TestCollectorFailureIsolation:
    def test_enqueues_candidates_from_other_collectors_when_one_raises(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        failing = _FakeCollector("broken", error=CollectorError("boom"))
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[failing, working],
        )

        # Assert
        assert result.enqueued_count == 1
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1
        assert jobs[0].payload["url"] == "https://example.com/articles/ok"

    def test_enqueues_candidates_from_other_collectors_when_one_raises_an_unexpected_error(
        self, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: `CollectorError` に限らず想定外の例外も1コレクター分の失敗として扱う。"""
        # Arrange
        failing = _FakeCollector("broken", error=RuntimeError("unexpected"))
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[failing, working],
        )

        # Assert
        assert result.enqueued_count == 1


class TestFreshnessFilter:
    def test_excludes_candidates_without_a_published_date(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        collector = _FakeCollector(
            "fake",
            [
                make_candidate("https://example.com/articles/no-date", published_at=None),
                make_candidate("https://example.com/articles/dated"),
            ],
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 1
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert jobs[0].payload["url"] == "https://example.com/articles/dated"

    def test_excludes_candidates_older_than_freshness_days(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        old_candidate = make_candidate(
            "https://example.com/articles/old", published_at=NOW - timedelta(days=30)
        )
        collector = _FakeCollector("fake", [old_candidate])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(freshness_days=7),
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 0


class TestExistingArticleExclusion:
    def test_excludes_candidates_already_stored_as_articles(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        existing_url = "https://example.com/articles/existing"
        make_article(db_session, existing_url)
        collector = _FakeCollector(
            "fake",
            [
                make_candidate(existing_url),
                make_candidate("https://example.com/articles/new"),
            ],
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 1
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert jobs[0].payload["url"] == "https://example.com/articles/new"


class TestAlreadyQueuedExclusion:
    def test_does_not_double_enqueue_when_a_pending_fetch_article_job_exists(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        queued_url = "https://example.com/articles/queued"
        db_session.add(
            Job(
                type=JobType.FETCH_ARTICLE.value,
                payload={"url": queued_url, "origin": CRAWL_ORIGIN},
                status=JobStatus.PENDING.value,
            )
        )
        db_session.flush()
        collector = _FakeCollector("fake", [make_candidate(queued_url)])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[collector],
        )

        # Assert — 既存の1件のまま増えない
        assert result.enqueued_count == 0
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert len(jobs) == 1


class TestDeduplication:
    def test_dedupes_candidates_with_the_same_normalized_url(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — トラッキングパラメータ違いだけの同一記事
        collector = _FakeCollector(
            "fake",
            [
                make_candidate("https://example.com/articles/a?utm_source=x"),
                make_candidate("https://example.com/articles/a?utm_source=y"),
            ],
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 1


class TestMaxCandidatesLimit:
    def test_limits_enqueued_candidates_to_max_candidates_per_run(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        collector = _FakeCollector(
            "fake",
            [
                make_candidate("https://example.com/articles/1", published_at=NOW),
                make_candidate(
                    "https://example.com/articles/2", published_at=NOW - timedelta(hours=1)
                ),
            ],
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(max_candidates_per_run=1),
            collectors=[collector],
        )

        # Assert — より新しい候補が優先して残る
        assert result.enqueued_count == 1
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        assert jobs[0].payload["url"] == "https://example.com/articles/1"


class TestSourceDomainScoping:
    def test_keeps_only_candidates_matching_the_source_domain_including_subdomains(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange
        collector = _FakeCollector(
            "fake",
            [
                make_candidate("https://example.com/articles/root"),
                make_candidate("https://blog.example.com/articles/sub"),
                make_candidate("https://other.com/articles/unrelated"),
            ],
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            source_domain="example.com",
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 2
        jobs = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).all()
        urls = {job.payload["url"] for job in jobs}
        assert urls == {
            "https://example.com/articles/root",
            "https://blog.example.com/articles/sub",
        }

    def test_does_not_match_a_look_alike_domain_via_substring(
        self, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: `evil-example.com` は `source_domain=example.com` に一致しない。"""
        # Arrange
        collector = _FakeCollector(
            "fake", [make_candidate("https://evil-example.com/articles/phishing")]
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            source_domain="example.com",
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 0

    def test_excludes_a_candidate_with_no_hostname_when_scoped_to_a_domain(
        self, db_session: Session, settings: Settings
    ) -> None:
        """ホストを取り出せない URL（相対パス等）は `source_domain` 指定時に除外される。"""
        # Arrange
        collector = _FakeCollector("fake", [make_candidate("/relative/path")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            source_domain="example.com",
            collectors=[collector],
        )

        # Assert
        assert result.enqueued_count == 0


class TestBraveSearchDisabled:
    def test_does_not_raise_and_other_collectors_still_run(self, db_session: Session) -> None:
        """受入基準: Brave の API キー未設定時でも例外にならず他コレクターが動作する。"""
        # Arrange
        disabled_settings = Settings(_env_file=None, brave_search_api_key=None)
        brave = BraveSearchCollector(("Kubernetes 解説",), disabled_settings)
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=disabled_settings,
            feeds_config=make_feeds_config(),
            collectors=[brave, working],
        )

        # Assert
        assert result.enqueued_count == 1


class TestCollectResultCounts:
    def test_reports_collected_excluded_and_enqueued_counts(
        self, db_session: Session, settings: Settings
    ) -> None:
        # Arrange — 1件は日付なしで除外、1件はそのまま enqueue される
        collector = _FakeCollector(
            "fake",
            [
                make_candidate("https://example.com/articles/no-date", published_at=None),
                make_candidate("https://example.com/articles/ok"),
            ],
        )

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[collector],
        )

        # Assert
        assert result.collected_count == 2
        assert result.excluded_count == 1
        assert result.enqueued_count == 1


class TestEnqueuedPayloadMarksCrawlOrigin:
    def test_enqueued_payload_has_no_registration_id_and_marks_the_crawl_origin(
        self, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: 巡回由来のジョブは registration_id を持たず、origin で判別できる。"""
        # Arrange
        collector = _FakeCollector("fake", [make_candidate("https://example.com/articles/ok")])

        # Act
        collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[collector],
        )

        # Assert
        job = db_session.scalars(select(Job).where(Job.type == JobType.FETCH_ARTICLE.value)).one()
        assert "registration_id" not in job.payload
        assert job.payload["origin"] == CRAWL_ORIGIN


class TestBuildDefaultCollectors:
    """`collectors` 省略時に `_build_default_collectors` が組み立てる内容を検証する。

    実コレクターのコンストラクタ自体は外部通信をしないが、`collect()` が万一
    呼ばれても実 HTTP に出ないよう、各クラスをフェイクへ差し替えたうえで検証する。
    """

    def test_skips_brave_and_its_interest_term_query_when_disabled(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """設計判断の確認: Brave 無効時はコレクター自体を構築せず、
        interest term 収集用の DB 問い合わせも発生しない。
        """
        # Arrange
        built_names: list[str] = []
        monkeypatch.setattr(service, "RssCollector", _stub_collector_factory("rss", built_names))
        monkeypatch.setattr(
            service, "JpMediaCollector", _stub_collector_factory("jp_media", built_names)
        )
        monkeypatch.setattr(
            service, "HackerNewsCollector", _stub_collector_factory("hacker_news", built_names)
        )
        monkeypatch.setattr(
            service,
            "GitHubReleasesCollector",
            _stub_collector_factory("github_releases", built_names),
        )
        monkeypatch.setattr(
            service, "ArxivCollector", _stub_collector_factory("arxiv", built_names)
        )
        monkeypatch.setattr(
            service,
            "DiscoveredFeedCollector",
            _stub_collector_factory("discovered_feeds", built_names),
        )
        monkeypatch.setattr(
            service, "BraveSearchCollector", _stub_collector_factory("brave_search", built_names)
        )
        disabled_settings = Settings(_env_file=None, brave_search_api_key=None)

        # Act
        result = collect_candidates(
            db_session, settings=disabled_settings, feeds_config=make_feeds_config()
        )

        # Assert
        assert "brave_search" not in built_names
        assert set(built_names) == {
            "rss",
            "jp_media",
            "hacker_news",
            "github_releases",
            "arxiv",
            "discovered_feeds",
        }
        assert result.collected_count == 0

    def test_builds_brave_with_queries_from_recent_analyzed_articles_when_enabled(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Brave 有効時は直近の解析済み記事の topics/technologies からクエリを組み立てて渡す。"""
        # Arrange
        analyzed_article = Article(
            canonical_url="https://example.com/analyzed",
            original_url="https://example.com/analyzed",
            title="解析済み記事",
            source_domain="example.com",
            analysis_status=JobStatus.COMPLETED.value,
            topics=["Kubernetes"],
            technologies=["Istio"],
        )
        db_session.add(analyzed_article)
        db_session.flush()

        received_queries: list[tuple[str, ...]] = []

        def fake_brave(
            queries: Sequence[str], _settings: Settings, **_kwargs: object
        ) -> _FakeCollector:
            received_queries.append(tuple(queries))
            return _FakeCollector("brave_search")

        monkeypatch.setattr(service, "RssCollector", _stub_collector_factory("rss", []))
        monkeypatch.setattr(service, "JpMediaCollector", _stub_collector_factory("jp_media", []))
        monkeypatch.setattr(
            service, "HackerNewsCollector", _stub_collector_factory("hacker_news", [])
        )
        monkeypatch.setattr(
            service, "GitHubReleasesCollector", _stub_collector_factory("github_releases", [])
        )
        monkeypatch.setattr(service, "ArxivCollector", _stub_collector_factory("arxiv", []))
        monkeypatch.setattr(
            service, "DiscoveredFeedCollector", _stub_collector_factory("discovered_feeds", [])
        )
        monkeypatch.setattr(service, "BraveSearchCollector", fake_brave)
        enabled_settings = Settings(_env_file=None, brave_search_api_key="dummy-key")

        # Act
        collect_candidates(db_session, settings=enabled_settings, feeds_config=make_feeds_config())

        # Assert — 記事の topics/technologies に基づくクエリが生成されている
        assert len(received_queries) == 1
        assert received_queries[0] != ()


class TestFeedDiscoveryIntegration:
    """`collect_candidates` からのフィード自動発見（Issue #93）呼び出しを検証する。"""

    def test_feed_discovery_failure_does_not_break_candidate_collection(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 発見処理が想定外の例外を送出しても、巡回自体（候補の enqueue）は成功する。"""
        # Arrange
        monkeypatch.setattr(
            service,
            "discover_feeds",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("discovery boom")),
        )
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[working],
        )

        # Assert
        assert result.enqueued_count == 1

    def test_calls_discover_feeds_with_the_default_user_id(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        received: list[dict[str, object]] = []

        def fake_discover_feeds(session: Session, *, user_id: object, settings: Settings) -> int:
            received.append({"session": session, "user_id": user_id, "settings": settings})
            return 0

        monkeypatch.setattr(service, "discover_feeds", fake_discover_feeds)
        working = _FakeCollector("working", [])

        # Act
        collect_candidates(
            db_session, settings=settings, feeds_config=make_feeds_config(), collectors=[working]
        )

        # Assert
        assert len(received) == 1
        assert received[0]["user_id"] == settings.default_user_id
        assert received[0]["session"] is db_session

    def test_a_failed_discovery_query_does_not_poison_the_session(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 発見処理が DB エラーで落ちても、巡回のトランザクションは使える状態で残る。

        PostgreSQL はエラーが起きたトランザクションを中断状態にするため、savepoint で
        囲っていないと、以降のクエリも呼び出し元の commit もすべて失敗し、enqueue 済みの
        候補まで巻き添えで消える。ここでは実際に DB 側のエラー（ゼロ除算）を起こし、
        その後もセッションが使えることを確かめる。
        """
        # Arrange
        from sqlalchemy import text

        def raise_a_database_error(session: Session, **kwargs: object) -> int:
            session.execute(text("SELECT 1 / 0"))
            return 0

        monkeypatch.setattr(service, "discover_feeds", raise_a_database_error)
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[working],
        )

        # Assert — enqueue は残り、セッションは中断状態になっていない
        assert result.enqueued_count == 1
        db_session.flush()
        assert len(db_session.scalars(select(Job)).all()) == 1


class TestFeedHealthRecording:
    """`collect_candidates` からの発見済みフィード死活監視反映（Issue #105）を検証する。

    `DiscoveredFeedCollector` は `RssCollector` の `fetch_resource` 呼び出しを
    そのまま使うため（`DiscoveredFeedCollector` 自体は取得処理を上書きしない）、
    `techradar.collectors.rss` モジュール内の `fetch_resource` 参照をスタブに
    差し替えて実 HTTP を出さないようにする（`test_collectors_discovery.py` の
    `TestDiscoveredFeedCollector` と同じ方針）。
    """

    def _make_discovered_feed_collector(
        self,
        monkeypatch: pytest.MonkeyPatch,
        settings: Settings,
        *,
        feed_url: str,
        succeeds: bool,
    ):  # type: ignore[no-untyped-def]
        from techradar.collectors import rss as rss_module
        from techradar.collectors.config import FeedEntryConfig
        from techradar.collectors.discovery import DiscoveredFeedCollector
        from techradar.fetcher.errors import FetchError
        from techradar.fetcher.http import FetchedResource

        fake = _FakeFetchResourceForRss()
        monkeypatch.setattr(rss_module, "fetch_resource", fake)
        feed = FeedEntryConfig(name="discovered.example.com", url=feed_url)
        if succeeds:
            fake.set_response(
                feed_url,
                FetchedResource(
                    final_url=feed_url,
                    body=b"<rss><channel></channel></rss>",
                    text="<rss><channel></channel></rss>",
                    content_type="application/rss+xml",
                    status_code=200,
                ),
            )
        else:
            fake.set_error(feed_url, FetchError("取得に失敗しました"))
        return DiscoveredFeedCollector([feed], settings)

    def test_records_feed_health_after_collecting(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 巡回後に `DiscoveredFeedCollector` の成否が
        `discovered_feeds` へ反映される。
        """
        # Arrange
        from techradar.db.enums import DiscoveredFeedStatus
        from techradar.db.models import DiscoveredFeed

        feed_url = "https://discovered.example.com/feed.xml"
        row = DiscoveredFeed(
            domain="discovered.example.com",
            feed_url=feed_url,
            status=DiscoveredFeedStatus.FOUND.value,
            article_count=1,
            last_attempted_at=NOW,
            enabled=True,
        )
        db_session.add(row)
        db_session.flush()

        discovered_collector = self._make_discovered_feed_collector(
            monkeypatch, settings, feed_url=feed_url, succeeds=False
        )
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[working, discovered_collector],
        )

        # Assert — 候補の enqueue は成功し、失敗も反映されている
        assert result.enqueued_count == 1
        db_session.refresh(row)
        assert row.consecutive_failures == 1

    def test_frees_a_discovery_slot_within_the_same_collect_run(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 無効化した分の枠は、同じ `collect_candidates` の呼び出し中に空く。

        `collect_candidates` は死活監視の反映を `_collect_all` の直後に置き、新規発見
        （`_discover_new_feeds_safely`）を末尾に置いている（docstring の処理順序）。
        反映が末尾より後になると、無効化した枠をその巡回では使えない。両者を個別に
        検証するだけではこの順序が保証されないため、1 回の呼び出しを通して確かめる。
        """
        # Arrange — 次の 1 回の失敗で閾値に達する FOUND 行
        from techradar.collectors.discovery import (
            MAX_CONSECUTIVE_FEED_FAILURES,
            _available_slots,
        )
        from techradar.db.enums import DiscoveredFeedStatus
        from techradar.db.models import DiscoveredFeed

        feed_url = "https://discovered.example.com/feed.xml"
        db_session.add(
            DiscoveredFeed(
                domain="discovered.example.com",
                feed_url=feed_url,
                status=DiscoveredFeedStatus.FOUND.value,
                article_count=1,
                last_attempted_at=NOW,
                enabled=True,
                consecutive_failures=MAX_CONSECUTIVE_FEED_FAILURES - 1,
            )
        )
        db_session.flush()
        slots_before = _available_slots(db_session)

        discovered_collector = self._make_discovered_feed_collector(
            monkeypatch, settings, feed_url=feed_url, succeeds=False
        )

        # 新規発見の中で残り枠を観測し、反映が済んだ後に呼ばれていることを確かめる
        observed_slots: list[int] = []

        def _spy_discover_feeds(session: Session, **kwargs: object) -> int:
            observed_slots.append(_available_slots(session))
            return 0

        monkeypatch.setattr(service, "discover_feeds", _spy_discover_feeds)
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[working, discovered_collector],
        )

        # Assert — 新規発見の時点で、無効化した 1 件ぶんの枠が空いている
        assert observed_slots == [slots_before + 1]

    def test_feed_health_recording_failure_does_not_break_candidate_collection(
        self, db_session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 反映処理が想定外の例外を送出しても、巡回自体（候補の enqueue）は成功する
        （savepoint の効果）。
        """
        # Arrange
        from techradar.db.enums import DiscoveredFeedStatus
        from techradar.db.models import DiscoveredFeed

        feed_url = "https://discovered.example.com/feed.xml"
        db_session.add(
            DiscoveredFeed(
                domain="discovered.example.com",
                feed_url=feed_url,
                status=DiscoveredFeedStatus.FOUND.value,
                article_count=1,
                last_attempted_at=NOW,
                enabled=True,
            )
        )
        db_session.flush()

        discovered_collector = self._make_discovered_feed_collector(
            monkeypatch, settings, feed_url=feed_url, succeeds=True
        )
        monkeypatch.setattr(
            service,
            "record_feed_health",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("health boom")),
        )
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        result = collect_candidates(
            db_session,
            settings=settings,
            feeds_config=make_feeds_config(),
            collectors=[working, discovered_collector],
        )

        # Assert
        assert result.enqueued_count == 1
        db_session.flush()
        assert len(db_session.scalars(select(Job)).all()) == 1

    def test_does_not_record_health_for_a_manual_feeds_yaml_collector(
        self, db_session: Session, settings: Settings
    ) -> None:
        """受入基準: `feeds.yaml` 由来の `RssCollector` は対象外
        （自動追加の枠を消費せず無効化する動機が無いため）。
        """
        # Arrange

        from techradar.collectors.rss import RssCollector

        called: list[object] = []
        original_record_feed_health = service.record_feed_health

        def spy_record_feed_health(
            session: Session,
            feed_results: dict[str, FeedFetchResult],
            *,
            now: datetime | None = None,
        ) -> None:
            called.append(True)
            original_record_feed_health(session, feed_results, now=now)

        manual_rss = RssCollector([], settings)
        working = _FakeCollector("working", [make_candidate("https://example.com/articles/ok")])

        # Act
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(service, "record_feed_health", spy_record_feed_health)
            collect_candidates(
                db_session,
                settings=settings,
                feeds_config=make_feeds_config(),
                collectors=[working, manual_rss],
            )

        # Assert — 対象の DiscoveredFeedCollector が無いので反映関数は呼ばれない
        assert called == []


class _FakeFetchResourceForRss:
    """`techradar.collectors.rss.fetch_resource` を差し替える小さなヘルパー。

    `test_collectors_rss.py` / `test_collectors_discovery.py` の
    `_FakeFetchResource` と同じ方針だが、循環 import を避けるためこのファイルに
    独立して定義する。
    """

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}

    def set_response(self, url: str, resource: object) -> None:
        self.responses[url] = resource

    def set_error(self, url: str, error: Exception) -> None:
        self.responses[url] = error

    def __call__(
        self, url: str, *, allowed_content_types: tuple[str, ...], settings: Settings | None = None
    ) -> object:
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result
