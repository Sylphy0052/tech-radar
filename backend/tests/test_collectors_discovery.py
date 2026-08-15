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
    MAX_DISCOVERED_FEEDS_TOTAL,
    MAX_DISCOVERY_DOMAINS_PER_RUN,
    DiscoveredFeedCollector,
    DomainCount,
    aggregate_domain_counts,
    discover_feeds,
    load_enabled_discovered_feeds,
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
) -> DiscoveredFeed:
    row = DiscoveredFeed(
        domain=domain,
        feed_url=feed_url,
        status=status.value,
        article_count=article_count,
        last_attempted_at=last_attempted_at,
        enabled=enabled,
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
