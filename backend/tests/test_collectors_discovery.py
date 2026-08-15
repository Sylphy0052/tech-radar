"""`techradar.collectors.discovery` の振る舞いを検証する（Issue #93）。

登録記事のドメインを集計し、上位ドメインから RSS/Atom フィードを自動発見して
`discovered_feeds` へ反映する処理をテストする。実 HTTP は一切出さない
（`tests/test_collectors_rss.py` の `_FakeFetchResource` と同じ差し替え方針）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.collectors import discovery as discovery_module
from techradar.collectors.discovery import (
    MAX_CONSECUTIVE_FEED_FAILURES,
    MAX_DISCOVERED_FEEDS_TOTAL,
    MAX_DISCOVERY_DOMAINS_PER_RUN,
    MAX_FEED_CANDIDATES_PER_DOMAIN,
    DiscoveredFeedCollector,
    DomainCount,
    _available_slots,
    aggregate_domain_counts,
    discover_feeds,
    load_enabled_discovered_feeds,
    record_feed_health,
    select_discovery_targets,
)
from techradar.config import Settings
from techradar.db.enums import ArticleOrigin, DiscoveredFeedStatus
from techradar.db.models import Article, DiscoveredFeed, UserArticle
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import FEED_CONTENT_TYPES, HTML_CONTENT_TYPES, FetchedResource
from techradar.fetcher.url import normalize_url

NOW = datetime(2026, 8, 15, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def make_article(session: Session, url: str, *, source_domain: str | None = None) -> Article:
    article = Article(
        canonical_url=normalize_url(url),
        original_url=url,
        title="記事",
        source_domain=source_domain if source_domain is not None else "example.com",
    )
    session.add(article)
    session.flush()
    return article


def make_user_article(
    session: Session, article: Article, *, user_id: uuid.UUID, origin: ArticleOrigin
) -> UserArticle:
    row = UserArticle(
        user_id=user_id,
        article_id=article.id,
        origin=origin.value,
        interest_weight=1.0,
    )
    session.add(row)
    session.flush()
    return row


def make_discovered_feed(
    session: Session,
    *,
    domain: str,
    status: DiscoveredFeedStatus,
    last_attempted_at: datetime,
    feed_url: str | None = None,
    enabled: bool = False,
    article_count: int = 1,
    consecutive_failures: int = 0,
    last_succeeded_at: datetime | None = None,
) -> DiscoveredFeed:
    row = DiscoveredFeed(
        domain=domain,
        feed_url=feed_url,
        status=status.value,
        article_count=article_count,
        last_attempted_at=last_attempted_at,
        enabled=enabled,
        consecutive_failures=consecutive_failures,
        last_succeeded_at=last_succeeded_at,
    )
    session.add(row)
    session.flush()
    return row


def _resource(text: str, *, final_url: str, content_type: str = "text/html") -> FetchedResource:
    return FetchedResource(
        final_url=final_url,
        body=text.encode("utf-8"),
        text=text,
        content_type=content_type,
        status_code=200,
    )


VALID_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Example Feed</title>
<link>https://example.com/</link>
<item>
<title>記事</title>
<link>https://example.com/articles/1</link>
</item>
</channel>
</rss>
"""

BROKEN_FEED_XML = "<not-a-feed>"


class _FakeFetchResource:
    """URL→応答を積み上げる小さなヘルパー（`test_collectors_rss.py` と同じ方針）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str, object] = {}

    def set_response(self, url: str, resource: FetchedResource) -> None:
        self.responses[url] = resource

    def set_error(self, url: str, error: Exception) -> None:
        self.responses[url] = error

    def __call__(
        self, url: str, *, allowed_content_types: tuple[str, ...], settings: Settings | None = None
    ) -> FetchedResource:
        self.calls.append({"url": url, "allowed_content_types": allowed_content_types})
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FetchedResource)
        return result


@pytest.fixture
def fake_fetch_resource(monkeypatch: pytest.MonkeyPatch) -> _FakeFetchResource:
    fake = _FakeFetchResource()
    monkeypatch.setattr(discovery_module, "fetch_resource", fake)
    return fake


class TestAggregateDomainCounts:
    def test_counts_by_domain_and_orders_by_count_descending(self, db_session: Session) -> None:
        # Arrange — b.example.com を2件、a.example.com を1件登録する
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(db_session, "https://a.example.com/1", source_domain="a.example.com"),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        make_user_article(
            db_session,
            make_article(db_session, "https://b.example.com/1", source_domain="b.example.com"),
            user_id=user_id,
            origin=ArticleOrigin.GOOD,
        )
        make_user_article(
            db_session,
            make_article(db_session, "https://b.example.com/2", source_domain="b.example.com"),
            user_id=user_id,
            origin=ArticleOrigin.SAVED,
        )

        # Act
        result = aggregate_domain_counts(db_session, user_id)

        # Assert — 件数降順、b.example.com が先頭
        assert result[0] == DomainCount(domain="b.example.com", article_count=2)
        assert result[1] == DomainCount(domain="a.example.com", article_count=1)

    def test_excludes_read_full_and_clicked_origins(self, db_session: Session) -> None:
        """受入基準: 集計対象は manual/good/saved の3経路のみ（read_full/clicked は除外）。"""
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://read.example.com/1", source_domain="read.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.READ_FULL,
        )
        make_user_article(
            db_session,
            make_article(
                db_session, "https://clicked.example.com/1", source_domain="clicked.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.CLICKED,
        )

        # Act
        result = aggregate_domain_counts(db_session, user_id)

        # Assert
        assert result == ()

    def test_excludes_other_users_registrations(self, db_session: Session) -> None:
        # Arrange
        target_user = uuid.uuid4()
        other_user = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://mine.example.com/1", source_domain="mine.example.com"
            ),
            user_id=target_user,
            origin=ArticleOrigin.MANUAL,
        )
        make_user_article(
            db_session,
            make_article(
                db_session, "https://other.example.com/1", source_domain="other.example.com"
            ),
            user_id=other_user,
            origin=ArticleOrigin.MANUAL,
        )

        # Act
        result = aggregate_domain_counts(db_session, target_user)

        # Assert
        assert result == (DomainCount(domain="mine.example.com", article_count=1),)


class TestSelectDiscoveryTargets:
    def test_excludes_domains_in_the_excluded_set(self) -> None:
        # Arrange
        ranked = (
            DomainCount(domain="a.com", article_count=3),
            DomainCount(domain="b.com", article_count=2),
        )

        # Act
        result = select_discovery_targets(ranked, excluded={"a.com"}, max_domains=5)

        # Assert
        assert result == (DomainCount(domain="b.com", article_count=2),)

    def test_limits_to_max_domains(self) -> None:
        # Arrange
        ranked = tuple(DomainCount(domain=f"d{i}.com", article_count=i) for i in range(10))

        # Act
        result = select_discovery_targets(ranked, excluded=set(), max_domains=3)

        # Assert
        assert len(result) == 3
        assert [d.domain for d in result] == ["d0.com", "d1.com", "d2.com"]


class TestDiscoverFeeds:
    def test_creates_a_found_row_when_feed_link_is_discovered(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://blog.example.com/1", source_domain="blog.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://blog.example.com/",
            _resource(homepage_html, final_url="https://blog.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://blog.example.com/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://blog.example.com/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 1
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "blog.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.feed_url == "https://blog.example.com/feed.xml"
        assert row.enabled is True
        assert row.article_count == 1
        assert row.last_attempted_at == NOW

    def test_records_not_found_when_no_feed_link_exists(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://nofeed.example.com/1", source_domain="nofeed.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        fake_fetch_resource.set_response(
            "https://nofeed.example.com/",
            _resource(
                "<html><head></head><body></body></html>", final_url="https://nofeed.example.com/"
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 0
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "nofeed.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.NOT_FOUND.value
        assert row.feed_url is None
        assert row.enabled is False

    def test_records_fetch_failed_when_homepage_cannot_be_fetched(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://down.example.com/1", source_domain="down.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        fake_fetch_resource.set_error("https://down.example.com/", FetchError("取得に失敗しました"))

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 0
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "down.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.FETCH_FAILED.value
        assert row.enabled is False

    def test_ignores_http_only_candidate_and_falls_back_to_not_found(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: `FeedEntryConfig` の https 限定制約と整合させ、
        http のフィード URL は採用しない。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://insecure.example.com/1", source_domain="insecure.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="http://insecure.example.com/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://insecure.example.com/",
            _resource(homepage_html, final_url="https://insecure.example.com/"),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — http 候補の feed_url へは fetch していない（採用も検証もしない）
        assert "http://insecure.example.com/feed.xml" not in [
            call["url"] for call in fake_fetch_resource.calls
        ]
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "insecure.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.NOT_FOUND.value

    def test_falls_back_to_next_candidate_when_first_feed_link_is_broken(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        # Arrange — 1件目のフィードリンクはパース不能、2件目は正常
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://multi.example.com/1", source_domain="multi.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="/broken.xml">'
            '<link rel="alternate" type="application/atom+xml" href="/healthy.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://multi.example.com/",
            _resource(homepage_html, final_url="https://multi.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://multi.example.com/broken.xml",
            _resource(
                BROKEN_FEED_XML,
                final_url="https://multi.example.com/broken.xml",
                content_type="application/rss+xml",
            ),
        )
        fake_fetch_resource.set_response(
            "https://multi.example.com/healthy.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://multi.example.com/healthy.xml",
                content_type="application/atom+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 1
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "multi.example.com")
        ).one()
        assert row.feed_url == "https://multi.example.com/healthy.xml"

    def test_resolves_relative_feed_href_against_the_final_url(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        # Arrange — リダイレクト後の final_url を基準に相対 URL を解決する
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://redir.example.com/1", source_domain="redir.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            '<html><head><link rel="alternate" type="application/rss+xml" href="feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://redir.example.com/",
            _resource(homepage_html, final_url="https://redir.example.com/blog/"),
        )
        fake_fetch_resource.set_response(
            "https://redir.example.com/blog/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://redir.example.com/blog/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "redir.example.com")
        ).one()
        assert row.feed_url == "https://redir.example.com/blog/feed.xml"

    def test_never_retries_a_domain_already_marked_as_found(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        # Arrange — 既に FOUND のドメインは何日経っていても候補から外れる
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://known.example.com/1", source_domain="known.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        make_discovered_feed(
            db_session,
            domain="known.example.com",
            status=DiscoveredFeedStatus.FOUND,
            last_attempted_at=NOW - timedelta(days=1000),
            feed_url="https://known.example.com/feed.xml",
            enabled=True,
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — 再度 HTTP を出していない
        assert fake_fetch_resource.calls == []

    def test_does_not_retry_a_domain_within_the_cooldown_period(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: NOT_FOUND/FETCH_FAILED から30日未満は再試行しない。"""
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session,
                "https://recent-fail.example.com/1",
                source_domain="recent-fail.example.com",
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        make_discovered_feed(
            db_session,
            domain="recent-fail.example.com",
            status=DiscoveredFeedStatus.NOT_FOUND,
            last_attempted_at=NOW - timedelta(days=29),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert fake_fetch_resource.calls == []

    def test_retries_a_domain_after_the_cooldown_period_has_passed(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 30日以上経過した NOT_FOUND/FETCH_FAILED は再試行し、行を更新する。"""
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://old-fail.example.com/1", source_domain="old-fail.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        make_discovered_feed(
            db_session,
            domain="old-fail.example.com",
            status=DiscoveredFeedStatus.NOT_FOUND,
            last_attempted_at=NOW - timedelta(days=31),
            article_count=1,
        )
        homepage_html = (
            '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://old-fail.example.com/",
            _resource(homepage_html, final_url="https://old-fail.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://old-fail.example.com/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://old-fail.example.com/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — 同じ行が更新される（新規行は増えない）
        assert found_count == 1
        rows = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "old-fail.example.com")
        ).all()
        assert len(rows) == 1
        assert rows[0].status == DiscoveredFeedStatus.FOUND.value
        assert rows[0].last_attempted_at == NOW

    def test_resets_the_failure_counter_when_a_disabled_domain_is_rediscovered(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 復活したフィードは失敗回数を持ち越さない（Issue #105）。

        連続失敗で DISABLED にした行はクールダウン後に再発見の対象へ戻る。その際に
        `consecutive_failures` を持ち越したままだと、復活直後の 1 回の失敗で閾値に
        達して即座に無効化され、「一時的な失敗では無効化しない」が復活後だけ
        成り立たなくなる。
        """
        # Arrange — 閾値まで失敗して無効化された行が、クールダウンを過ぎている
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://revived.example.com/1", source_domain="revived.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        make_discovered_feed(
            db_session,
            domain="revived.example.com",
            status=DiscoveredFeedStatus.DISABLED,
            last_attempted_at=NOW - timedelta(days=31),
            feed_url="https://revived.example.com/feed.xml",
            enabled=False,
            consecutive_failures=MAX_CONSECUTIVE_FEED_FAILURES,
        )
        homepage_html = (
            '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://revived.example.com/",
            _resource(homepage_html, final_url="https://revived.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://revived.example.com/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://revived.example.com/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — FOUND へ戻り、失敗回数は 0 から数え直す
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "revived.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.consecutive_failures == 0

        # Act — 復活後に 1 回失敗しても無効化されない
        record_feed_health(db_session, {"https://revived.example.com/feed.xml": False}, now=NOW)

        # Assert
        db_session.refresh(row)
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.consecutive_failures == 1

    def test_does_not_start_discovery_when_no_slots_are_available(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 自動追加総数上限に達していれば新規ドメインへの HTTP を一切出さない。"""
        # Arrange — 上限まで FOUND を積んでおく
        for i in range(MAX_DISCOVERED_FEEDS_TOTAL):
            make_discovered_feed(
                db_session,
                domain=f"existing{i}.example.com",
                status=DiscoveredFeedStatus.FOUND,
                last_attempted_at=NOW,
                feed_url=f"https://existing{i}.example.com/feed.xml",
                enabled=True,
            )
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(db_session, "https://new.example.com/1", source_domain="new.example.com"),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 0
        assert fake_fetch_resource.calls == []
        assert (
            db_session.scalars(
                select(DiscoveredFeed).where(DiscoveredFeed.domain == "new.example.com")
            ).one_or_none()
            is None
        )

    def test_limits_domains_tried_to_max_domains_per_run(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 1回の巡回で発見を試みるドメイン数は MAX_DISCOVERY_DOMAINS_PER_RUN 件まで。"""
        # Arrange — 上限より多いドメイン数を登録する
        user_id = uuid.uuid4()
        domain_count = MAX_DISCOVERY_DOMAINS_PER_RUN + 3
        for i in range(domain_count):
            domain = f"many{i:02d}.example.com"
            make_user_article(
                db_session,
                make_article(db_session, f"https://{domain}/1", source_domain=domain),
                user_id=user_id,
                origin=ArticleOrigin.MANUAL,
            )
            fake_fetch_resource.set_response(
                f"https://{domain}/",
                _resource(
                    "<html><head></head><body></body></html>", final_url=f"https://{domain}/"
                ),
            )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — 発見を試みたのは上限件数分だけ
        attempted_domains = {call["url"] for call in fake_fetch_resource.calls}
        assert len(attempted_domains) == MAX_DISCOVERY_DOMAINS_PER_RUN

    def test_feed_discovery_failure_for_one_domain_does_not_stop_others(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 1ドメインの発見処理で想定外の例外が起きても他のドメインの発見は続く。"""
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://broken.example.com/1", source_domain="broken.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.GOOD,
        )
        make_user_article(
            db_session,
            make_article(
                db_session, "https://healthy.example.com/1", source_domain="healthy.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        fake_fetch_resource.set_error("https://broken.example.com/", RuntimeError("想定外のバグ"))
        fake_fetch_resource.set_response(
            "https://healthy.example.com/",
            _resource(
                "<html><head></head><body></body></html>", final_url="https://healthy.example.com/"
            ),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — broken 側は FETCH_FAILED として記録され、healthy 側も処理される
        broken_row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "broken.example.com")
        ).one()
        assert broken_row.status == DiscoveredFeedStatus.FETCH_FAILED.value
        healthy_row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "healthy.example.com")
        ).one()
        assert healthy_row.status == DiscoveredFeedStatus.NOT_FOUND.value

    def test_uses_feed_content_types_when_validating_the_candidate_feed(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """SSRF ガード経路（fetch_resource）を経由し、
        許可 Content-Type がフィード用であることを担保する。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(db_session, "https://ct.example.com/1", source_domain="ct.example.com"),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://ct.example.com/", _resource(homepage_html, final_url="https://ct.example.com/")
        )
        fake_fetch_resource.set_response(
            "https://ct.example.com/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://ct.example.com/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        homepage_call = next(
            c for c in fake_fetch_resource.calls if c["url"] == "https://ct.example.com/"
        )
        assert homepage_call["allowed_content_types"] == HTML_CONTENT_TYPES
        feed_call = next(
            c for c in fake_fetch_resource.calls if c["url"] == "https://ct.example.com/feed.xml"
        )
        assert feed_call["allowed_content_types"] == FEED_CONTENT_TYPES

    def test_limits_feed_candidates_tried_per_domain(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 1ドメインから取得を試す候補は MAX_FEED_CANDIDATES_PER_DOMAIN 件まで。

        `<link rel="alternate">` を大量に並べた HTML を踏むと、上限が無ければ
        1ドメインの発見だけでジョブワーカーが長時間占有される。
        """
        # Arrange — 上限より多い候補を並べ、いずれもパースできないフィードを返す
        user_id = uuid.uuid4()
        domain = "manylinks.example.com"
        make_user_article(
            db_session,
            make_article(db_session, f"https://{domain}/1", source_domain=domain),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        candidate_count = MAX_FEED_CANDIDATES_PER_DOMAIN + 3
        links = "".join(
            f'<link rel="alternate" type="application/rss+xml" href="/feed{i}.xml">'
            for i in range(candidate_count)
        )
        fake_fetch_resource.set_response(
            f"https://{domain}/",
            _resource(
                f"<html><head>{links}</head><body></body></html>", final_url=f"https://{domain}/"
            ),
        )
        for i in range(candidate_count):
            feed_url = f"https://{domain}/feed{i}.xml"
            fake_fetch_resource.set_response(
                feed_url,
                _resource(BROKEN_FEED_XML, final_url=feed_url, content_type="application/rss+xml"),
            )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — トップページ以外への取得は上限件数で打ち切られる
        feed_calls = [c for c in fake_fetch_resource.calls if c["url"] != f"https://{domain}/"]
        assert len(feed_calls) == MAX_FEED_CANDIDATES_PER_DOMAIN
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == domain)
        ).one()
        assert row.status == DiscoveredFeedStatus.NOT_FOUND.value

    def test_ignores_feed_candidates_pointing_at_another_site(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 外部サイトの HTML が指す第三者の URL は巡回対象にしない。

        `<link rel="alternate">` の href には任意の URL を書けるため、制限が
        無いと「攻撃者が選んだ URL を恒久的な巡回先として登録させる」ことが
        できてしまう。取得すれば有効なフィードを返す応答を用意しておき、
        そもそも取得しないことを確かめる。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://linkfarm.example.com/1", source_domain="linkfarm.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" '
            'href="https://third-party.example.net/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://linkfarm.example.com/",
            _resource(homepage_html, final_url="https://linkfarm.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://third-party.example.net/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://third-party.example.net/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — 別サイトの候補へは取得へ行かず、発見できなかった扱いになる
        assert found_count == 0
        assert [call["url"] for call in fake_fetch_resource.calls] == [
            "https://linkfarm.example.com/"
        ]
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "linkfarm.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.NOT_FOUND.value

    def test_accepts_a_feed_candidate_on_a_subdomain_of_the_homepage(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 同じサイトの配下（サブドメイン）に置かれたフィードは採用する。

        フィード配信を `feeds.` のような別ホストへ分けているサイトを
        取りこぼさないため、同一ホスト完全一致までは狭めない。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://parent.example.com/1", source_domain="parent.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" '
            'href="https://feeds.parent.example.com/rss.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://parent.example.com/",
            _resource(homepage_html, final_url="https://parent.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://feeds.parent.example.com/rss.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://feeds.parent.example.com/rss.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 1
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "parent.example.com")
        ).one()
        assert row.feed_url == "https://feeds.parent.example.com/rss.xml"

    def test_accepts_link_type_with_parameters_and_uppercase_attributes(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: `type` のパラメータ付き表記・大文字表記を取りこぼさない。

        `type="application/rss+xml; charset=utf-8"` を出すサイトがあるため、
        `fetcher.http._check_content_type` と同じ正規化をしてから比較する。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://mixedcase.example.com/1", source_domain="mixedcase.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="Alternate" type="Application/RSS+XML; charset=utf-8" href="/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://mixedcase.example.com/",
            _resource(homepage_html, final_url="https://mixedcase.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://mixedcase.example.com/feed.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://mixedcase.example.com/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 1
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "mixedcase.example.com")
        ).one()
        assert row.feed_url == "https://mixedcase.example.com/feed.xml"

    def test_fetches_a_duplicated_candidate_url_only_once(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 同じ href が複数のタグに書かれていても取得は 1 回だけ。"""
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(db_session, "https://dup.example.com/1", source_domain="dup.example.com"),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
            '<link rel="alternate" type="application/atom+xml" href="/feed.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://dup.example.com/",
            _resource(homepage_html, final_url="https://dup.example.com/"),
        )
        fake_fetch_resource.set_response(
            "https://dup.example.com/feed.xml",
            _resource(
                BROKEN_FEED_XML,
                final_url="https://dup.example.com/feed.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        feed_calls = [
            c for c in fake_fetch_resource.calls if c["url"] == "https://dup.example.com/feed.xml"
        ]
        assert len(feed_calls) == 1

    def test_falls_back_to_next_candidate_when_the_first_feed_cannot_be_fetched(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 候補フィードの取得自体が失敗しても次の候補を試す。

        パース失敗（`bozo`）のフォールバックとは別の分岐（`FetchError`）を通す。
        """
        # Arrange
        user_id = uuid.uuid4()
        make_user_article(
            db_session,
            make_article(
                db_session, "https://deadlink.example.com/1", source_domain="deadlink.example.com"
            ),
            user_id=user_id,
            origin=ArticleOrigin.MANUAL,
        )
        homepage_html = (
            "<html><head>"
            '<link rel="alternate" type="application/rss+xml" href="/gone.xml">'
            '<link rel="alternate" type="application/rss+xml" href="/alive.xml">'
            "</head><body></body></html>"
        )
        fake_fetch_resource.set_response(
            "https://deadlink.example.com/",
            _resource(homepage_html, final_url="https://deadlink.example.com/"),
        )
        fake_fetch_resource.set_error(
            "https://deadlink.example.com/gone.xml", FetchError("404 が返りました")
        )
        fake_fetch_resource.set_response(
            "https://deadlink.example.com/alive.xml",
            _resource(
                VALID_FEED_XML,
                final_url="https://deadlink.example.com/alive.xml",
                content_type="application/rss+xml",
            ),
        )

        # Act
        found_count = discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert
        assert found_count == 1
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "deadlink.example.com")
        ).one()
        assert row.feed_url == "https://deadlink.example.com/alive.xml"

    def test_limits_domains_to_the_remaining_slots(
        self, db_session: Session, settings: Settings, fake_fetch_resource: _FakeFetchResource
    ) -> None:
        """受入基準: 総数上限までの残り枠が per-run 上限より少なければ、残り枠まで。

        枠 0（発見処理そのものを始めない）と枠十分（per-run 上限が効く）の間の、
        枠を部分的に消費した状態を通す。
        """
        # Arrange — 残り 2 枠になるまで FOUND を積み、per-run 上限ぶんのドメインを用意する
        remaining_slots = 2
        for i in range(MAX_DISCOVERED_FEEDS_TOTAL - remaining_slots):
            make_discovered_feed(
                db_session,
                domain=f"taken{i:02d}.example.com",
                status=DiscoveredFeedStatus.FOUND,
                last_attempted_at=NOW,
                feed_url=f"https://taken{i:02d}.example.com/feed.xml",
                enabled=True,
            )
        user_id = uuid.uuid4()
        for i in range(MAX_DISCOVERY_DOMAINS_PER_RUN):
            domain = f"slot{i:02d}.example.com"
            make_user_article(
                db_session,
                make_article(db_session, f"https://{domain}/1", source_domain=domain),
                user_id=user_id,
                origin=ArticleOrigin.MANUAL,
            )
            fake_fetch_resource.set_response(
                f"https://{domain}/",
                _resource(
                    "<html><head></head><body></body></html>", final_url=f"https://{domain}/"
                ),
            )

        # Act
        discover_feeds(db_session, user_id=user_id, settings=settings, now=NOW)

        # Assert — per-run 上限（5）ではなく残り枠（2）まで
        assert len({call["url"] for call in fake_fetch_resource.calls}) == remaining_slots

    def test_updates_the_existing_row_when_the_insert_races_with_another_writer(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受入基準: 一意制約違反を検知したら、既存行を新しい発見結果で更新する。

        単一ユーザー・ローカル実行では実際のレースはまず起きないため、
        「最初の検索だけ空を返す」状態を作ってこの分岐を通す。
        """
        # Arrange — DB には行があるが、最初の検索では見つからないことにする
        make_discovered_feed(
            db_session,
            domain="race.example.com",
            status=DiscoveredFeedStatus.NOT_FOUND,
            last_attempted_at=NOW - timedelta(days=365),
        )
        original_scalars = db_session.scalars
        seen_calls = {"count": 0}

        def scalars_hiding_the_first_result(statement, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen_calls["count"] += 1
            if seen_calls["count"] == 1:
                statement = select(DiscoveredFeed).where(
                    DiscoveredFeed.domain == "no-such-domain.example.com"
                )
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(db_session, "scalars", scalars_hiding_the_first_result)

        # Act
        discovery_module._upsert_discovered_feed(
            db_session,
            domain="race.example.com",
            feed_url="https://race.example.com/feed.xml",
            status=DiscoveredFeedStatus.FOUND.value,
            article_count=3,
            now=NOW,
        )

        # Assert — 挿入は諦め、既存行が新しい結果で上書きされる
        row = db_session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == "race.example.com")
        ).one()
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.feed_url == "https://race.example.com/feed.xml"
        assert row.article_count == 3
        assert row.enabled is True


class TestLoadEnabledDiscoveredFeeds:
    def test_returns_only_found_and_enabled_rows_as_feed_entry_config(
        self, db_session: Session
    ) -> None:
        # Arrange
        make_discovered_feed(
            db_session,
            domain="found-enabled.example.com",
            status=DiscoveredFeedStatus.FOUND,
            last_attempted_at=NOW,
            feed_url="https://found-enabled.example.com/feed.xml",
            enabled=True,
        )
        make_discovered_feed(
            db_session,
            domain="not-found.example.com",
            status=DiscoveredFeedStatus.NOT_FOUND,
            last_attempted_at=NOW,
            enabled=False,
        )

        # Act
        result = load_enabled_discovered_feeds(db_session)

        # Assert
        assert len(result) == 1
        assert result[0].name == "found-enabled.example.com"
        assert result[0].url == "https://found-enabled.example.com/feed.xml"


class TestDiscoveredFeedCollector:
    def test_collects_candidates_via_the_underlying_rss_parsing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`RssCollector` を継承し、パース処理を再利用していることを確認する。

        実際の取得呼び出しは継承元 `RssCollector._collect_feed` が持つ
        `techradar.collectors.rss` モジュール内の `fetch_resource` 参照を経由する
        （`DiscoveredFeedCollector` 自体は取得処理を上書きしないため）。
        """
        # Arrange
        from techradar.collectors import rss as rss_module
        from techradar.collectors.config import FeedEntryConfig

        fake = _FakeFetchResource()
        monkeypatch.setattr(rss_module, "fetch_resource", fake)
        feed = FeedEntryConfig(name="known.example.com", url="https://known.example.com/feed.xml")
        fake.set_response(
            feed.url,
            _resource(VALID_FEED_XML, final_url=feed.url, content_type="application/rss+xml"),
        )
        collector = DiscoveredFeedCollector([feed], settings)

        # Act
        candidates = collector.collect()

        # Assert
        assert collector.name == "discovered_feeds"
        assert len(candidates) == 1
        assert candidates[0].url == "https://example.com/articles/1"


class TestRecordFeedHealth:
    """`record_feed_health` の振る舞いを検証する（Issue #105、発見済みフィードの死活監視）。

    `DiscoveredFeedCollector.feed_results()` の戻り値をそのまま渡す想定で、
    キーはフィード URL、値は成否 (bool) の `dict` を渡す。
    """

    def test_does_not_disable_after_a_single_failure(self, db_session: Session) -> None:
        # Arrange
        feed_url = "https://once.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="once.example.com",
            status=DiscoveredFeedStatus.FOUND,
            last_attempted_at=NOW,
            feed_url=feed_url,
            enabled=True,
        )

        # Act
        record_feed_health(db_session, {feed_url: False}, now=NOW)

        # Assert
        db_session.refresh(row)
        assert row.consecutive_failures == 1
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.enabled is True

    def test_disables_after_reaching_the_consecutive_failure_threshold(
        self, db_session: Session
    ) -> None:
        # Arrange — 閾値未満まで失敗を積んでおく
        feed_url = "https://flaky.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="flaky.example.com",
            status=DiscoveredFeedStatus.FOUND,
            last_attempted_at=NOW,
            feed_url=feed_url,
            enabled=True,
            consecutive_failures=MAX_CONSECUTIVE_FEED_FAILURES - 1,
        )

        # Act — 閾値目の失敗
        record_feed_health(db_session, {feed_url: False}, now=NOW)

        # Assert
        db_session.refresh(row)
        assert row.consecutive_failures == MAX_CONSECUTIVE_FEED_FAILURES
        assert row.status == DiscoveredFeedStatus.DISABLED.value
        assert row.enabled is False

    def test_resets_the_counter_and_records_last_succeeded_at_on_success(
        self, db_session: Session
    ) -> None:
        # Arrange — 失敗を積んだ状態から成功させる
        feed_url = "https://recovered.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="recovered.example.com",
            status=DiscoveredFeedStatus.FOUND,
            last_attempted_at=NOW,
            feed_url=feed_url,
            enabled=True,
            consecutive_failures=2,
        )

        # Act
        record_feed_health(db_session, {feed_url: True}, now=NOW)

        # Assert
        db_session.refresh(row)
        assert row.consecutive_failures == 0
        assert row.last_succeeded_at == NOW
        assert row.status == DiscoveredFeedStatus.FOUND.value

    def test_frees_a_slot_after_disabling_a_feed(self, db_session: Session) -> None:
        """受入基準: 無効化した時点で `_available_slots`（自動追加総数上限の残り枠）が増える。

        `_available_slots` は `status == FOUND` だけを見ているため（discovery.py の
        `_available_slots`/`_already_found_domains`/`_cooldown_domains` docstring 参照）、
        DISABLED にした時点で枠が空き、同時にクールダウン経由で再発見の対象へ戻る。
        """
        # Arrange
        feed_url = "https://toremove.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="toremove.example.com",
            status=DiscoveredFeedStatus.FOUND,
            last_attempted_at=NOW,
            feed_url=feed_url,
            enabled=True,
            consecutive_failures=MAX_CONSECUTIVE_FEED_FAILURES - 1,
        )
        slots_before = _available_slots(db_session)

        # Act
        record_feed_health(db_session, {feed_url: False}, now=NOW)

        # Assert
        db_session.refresh(row)
        assert row.status == DiscoveredFeedStatus.DISABLED.value
        assert _available_slots(db_session) == slots_before + 1

    def test_updates_every_row_sharing_the_same_feed_url(self, db_session: Session) -> None:
        """受入基準: 同じ `feed_url` を持つ行が複数あれば、そのすべてへ成否を反映する。

        一意なのは `domain` だけで `feed_url` に一意制約は無い。別々のドメインの
        トップページが同じフィード URL を指していれば行が並ぶ（`_is_same_site` は
        トップページと同じホストかそのサブドメインであることしか求めないため、
        リダイレクト等でこの形は成立しうる）。1 行だけ更新すると、残りの行の成否が
        黙って捨てられて無効化の判定が狂う。
        """
        # Arrange — 同じ feed_url を指す 2 ドメイン。どちらも次の失敗で閾値へ届く
        feed_url = "https://shared.example.com/feed.xml"
        rows = [
            make_discovered_feed(
                db_session,
                domain=domain,
                status=DiscoveredFeedStatus.FOUND,
                last_attempted_at=NOW,
                feed_url=feed_url,
                enabled=True,
                consecutive_failures=MAX_CONSECUTIVE_FEED_FAILURES - 1,
            )
            for domain in ("shared.example.com", "blog.shared.example.com")
        ]

        # Act
        record_feed_health(db_session, {feed_url: False}, now=NOW)

        # Assert — 両方が無効化される
        for row in rows:
            db_session.refresh(row)
            assert row.status == DiscoveredFeedStatus.DISABLED.value
            assert row.enabled is False
            assert row.consecutive_failures == MAX_CONSECUTIVE_FEED_FAILURES

    def test_ignores_a_feed_url_with_no_matching_row(self, db_session: Session) -> None:
        """受入基準: 巡回中に行が消えた場合など、対象の行が見つからない URL は黙って無視する。"""
        # Act / Assert — 例外を送出しない
        record_feed_health(db_session, {"https://gone.example.com/feed.xml": False}, now=NOW)
