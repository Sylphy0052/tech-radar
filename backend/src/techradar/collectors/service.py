"""巡回サービス（`PROJECT_SPEC.md` §12, Issue #9 T13）。

各コレクター（RSS / 国内技術メディア / Hacker News / GitHub Releases / arXiv /
Brave Search）を束ねて実行し、鮮度・重複・既存記事・重複ジョブの各観点で
候補記事を絞り込んだうえで `fetch_article` ジョブとして enqueue する。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.collectors.arxiv import ArxivCollector
from techradar.collectors.base import CandidateArticle, SourceCollector
from techradar.collectors.brave import BraveSearchCollector
from techradar.collectors.config import FeedsConfig, get_feeds_config
from techradar.collectors.discovery import (
    DiscoveredFeedCollector,
    discover_feeds,
    load_enabled_discovered_feeds,
    record_feed_health,
    record_feed_novelty,
)
from techradar.collectors.filters import filter_recent, limit_candidates
from techradar.collectors.github_releases import GitHubReleasesCollector
from techradar.collectors.hackernews import HackerNewsCollector
from techradar.collectors.jp_media import JpMediaCollector
from techradar.collectors.query import build_search_queries
from techradar.collectors.rss import FeedFetchResult, RssCollector
from techradar.config import Settings, get_settings
from techradar.db.enums import JobStatus, JobType
from techradar.db.models import Article, Job
from techradar.fetcher.url import normalize_url
from techradar.jobs.queue import enqueue
from techradar.jobs.status import running_status_for

logger = logging.getLogger(__name__)

# Brave 検索クエリの元にする topics / technologies を、直近何件の解析済み記事から
# 集めるか。無制限に遡ると DB 走査コストと生成されるクエリ数の両方が膨らむため、
# コストの安全弁として上限を設ける（`PROJECT_SPEC.md` §24）。
INTEREST_SOURCE_ARTICLE_LIMIT = 20

# fetch_article ジョブの payload に入れる、巡回由来であることを示す値。
# 既存の JobType 名をそのまま使い回すことで、由来を表す文字列を新たに
# 増やさない。
CRAWL_ORIGIN = JobType.CRAWL_SOURCES.value

# 二重 enqueue 防止の判定対象にする fetch_article の status（pending または実行中）。
_ACTIVE_FETCH_ARTICLE_STATUSES = frozenset(
    {JobStatus.PENDING.value, running_status_for(JobType.FETCH_ARTICLE).value}
)


@dataclass(frozen=True)
class CollectResult:
    """1 回の巡回の結果件数。"""

    collected_count: int
    excluded_count: int
    enqueued_count: int


def collect_candidates(
    session: Session,
    *,
    settings: Settings | None = None,
    feeds_config: FeedsConfig | None = None,
    source_domain: str | None = None,
    collectors: Sequence[SourceCollector] | None = None,
) -> CollectResult:
    """コレクター群を実行し、絞り込んだ候補記事を `fetch_article` として積む。

    処理順序（早い段階の絞り込みほど、以降の重い処理を減らす）:
    1. 各コレクターの `collect()` を呼ぶ（1 つの失敗が他を巻き込まない）
    2. `DiscoveredFeedCollector` の巡回結果（成否）を `discovered_feeds` へ反映する
       （Issue #105、発見済みフィードの死活監視。`feeds.yaml` 由来の手動フィードは
       対象外）。ここで無効化した分だけ枠が空くため、同じ巡回の 10 で新しい
       ドメインを開拓できる
    3. `filter_recent` で公開日不明・古い候補を除外する
    4. `source_domain` が指定されていればホスト一致のものだけへ絞る
    5. 正規化 URL で候補内の重複を排除する
    6. 既に `articles` にある記事（正規化 URL 一致）を除外する
    7. 既に pending / 実行中の `fetch_article` ジョブがある URL を除外する
    8. ここまでの除外を通り抜けた候補の件数をフィード単位で `discovered_feeds` へ
       反映する（Issue #109、新着が出ないフィードの無効化。上限適用の前に数える
       理由と `source_domain` 指定時に飛ばす理由は `_record_feed_novelty_safely`）
    9. `max_candidates_per_run` で上限を適用する
    10. 残った候補を `fetch_article` として enqueue する
    11. 登録記事のドメインから新規巡回先を自動発見し `discovered_feeds` へ反映する
        （Issue #93、次回以降の巡回で拾われる。今回の収集結果には使わない）

    発見済みフィードへの反映を 2 と 8 の 2 段階に分けているのは、両者が別の問いを
    数えるため（ADR 0008）。2 は取得の成否と配信件数を、8 は除外後の新着件数を見る。
    どちらも新規発見（11）より前にあるので、無効化で空いた枠は同じ巡回で使える。
    """
    resolved_settings = settings or get_settings()
    resolved_feeds_config = feeds_config or get_feeds_config()
    resolved_collectors = (
        collectors
        if collectors is not None
        else _build_default_collectors(session, resolved_settings, resolved_feeds_config)
    )

    raw_candidates = _collect_all(resolved_collectors)
    _record_feed_health_safely(session, resolved_collectors)
    fresh = filter_recent(raw_candidates, freshness_days=resolved_feeds_config.freshness_days)
    scoped = _filter_by_source_domain(fresh, source_domain)
    deduped = _dedupe_by_normalized_url(scoped)
    unseen = _exclude_existing_articles(session, deduped)
    not_yet_queued = _exclude_already_queued(session, unseen)
    _record_feed_novelty_safely(
        session, resolved_collectors, not_yet_queued, source_domain=source_domain
    )
    limited = limit_candidates(
        not_yet_queued, max_candidates=resolved_feeds_config.max_candidates_per_run
    )

    enqueued_count = _enqueue_fetch_jobs(session, limited)

    result = CollectResult(
        collected_count=len(raw_candidates),
        excluded_count=len(raw_candidates) - enqueued_count,
        enqueued_count=enqueued_count,
    )
    logger.info(
        "collectors.service.collect_candidates_completed "
        "collected=%d excluded=%d enqueued=%d source_domain=%s",
        result.collected_count,
        result.excluded_count,
        result.enqueued_count,
        source_domain,
    )
    _discover_new_feeds_safely(session, settings=resolved_settings)
    return result


def _record_feed_health_safely(session: Session, collectors: Sequence[SourceCollector]) -> None:
    """`DiscoveredFeedCollector` の巡回結果を `discovered_feeds` へ反映する（Issue #105, #108）。

    `collectors` の中から `DiscoveredFeedCollector` のインスタンスだけを見つけて
    `feed_results()`（`RssCollector` が持つ、フィード URL ごとの結果の記録。
    `techradar.collectors.rss.RssCollector` / `FeedFetchResult` docstring 参照）を読み、まとめて
    `discovery.record_feed_health` へ渡す。`feeds.yaml` 由来の `RssCollector` /
    `JpMediaCollector`（`DiscoveredFeedCollector` のインスタンスではない）は
    対象にしない。自動追加の枠（`discovery.MAX_DISCOVERED_FEEDS_TOTAL`）を
    消費しないため無効化する動機が無く、人が意図して置いた手動フィードを
    機械が黙って外す方が危ういため（`discovery.record_feed_health` docstring
    にも同じ理由を記載）。

    `_discover_new_feeds_safely` と同じ理由で savepoint（`session.begin_nested()`）
    で囲み、例外はログのみに留める。`record_feed_health` は `discovered_feeds`
    へ書き込むため、その flush が失敗するとセッションが「rollback 待ち」の
    まま残り、呼び出し元の `session.commit()` が `PendingRollbackError` で
    落ちて、既に enqueue 済みの候補まで巻き添えで消えてしまう。savepoint を
    張っておけば、例外時に反映処理ぶんだけを巻き戻してセッションを使える
    状態へ戻せる。

    `DiscoveredFeedCollector` の判別（`isinstance`）も含めて丸ごと捕捉する。
    `_build_default_collectors` の各コレクタークラスをテスト用に差し替える
    ケース（`TestBuildDefaultCollectors`）では `DiscoveredFeedCollector` 自体が
    クラスではなくなるため、ここが失敗しても巡回結果を巻き込まないようにする。
    """
    try:
        feed_results = _discovered_feed_results(collectors)
        if not feed_results:
            return

        with session.begin_nested():
            record_feed_health(session, feed_results)
    except Exception:
        logger.warning("collectors.service.feed_health_recording_failed", exc_info=True)


def _record_feed_novelty_safely(
    session: Session,
    collectors: Sequence[SourceCollector],
    candidates: Sequence[CandidateArticle],
    *,
    source_domain: str | None,
) -> None:
    """除外を通り抜けた候補の件数を `discovered_feeds` へ反映する（Issue #109、ADR 0008）。

    `candidates` は `_exclude_already_queued` を通過した時点の候補。
    `limit_candidates` の**前**で数える。`max_candidates_per_run` で切られた分を
    「新着が無かった」と数えると、他フィードの新着が多い巡回で、上限に押し出された
    だけのフィードを無効化してしまうため。

    数える対象は、その巡回で取得・パースに成功した `DiscoveredFeedCollector` の
    フィードだけ（`record_feed_health` と同じく `feeds.yaml` 由来の手動フィードは
    対象外）。取得に失敗した回は候補が 0 件になるのが当然であり、「新着が無かった」
    と数えてはならない（`consecutive_failures` の側でのみ数える）。

    `source_domain` が指定された再巡回では、この反映を丸ごと飛ばす。
    `_filter_by_source_domain` が他ドメインの候補を全て落とすため、ここを数えると
    指定ドメイン以外の全フィードが新着 0 件に見え、単一ドメインの再巡回を繰り返す
    だけで無関係なフィードが軒並み無効化される。

    `_record_feed_health_safely` と同じ理由で savepoint（`session.begin_nested()`）
    で囲み、例外はログのみに留める（反映の失敗で enqueue 済みの候補を巻き添えに
    しない）。
    """
    if source_domain is not None:
        return

    try:
        feed_results = _discovered_feed_results(collectors)
        new_entry_counts = _count_new_entries_by_feed_url(feed_results, candidates)
        if not new_entry_counts:
            return

        with session.begin_nested():
            record_feed_novelty(session, new_entry_counts)
    except Exception:
        logger.warning("collectors.service.feed_novelty_recording_failed", exc_info=True)


def _discovered_feed_results(
    collectors: Sequence[SourceCollector],
) -> dict[str, FeedFetchResult]:
    """`DiscoveredFeedCollector` のフィード URL ごとの巡回結果をまとめて返す。

    `feeds.yaml` 由来の `RssCollector` / `JpMediaCollector`（`DiscoveredFeedCollector`
    のインスタンスではない）は対象にしない。`isinstance` 自体が失敗しうる
    （`_record_feed_health_safely` docstring 参照）ため、呼び出し側の try の中で使う。
    """
    feed_results: dict[str, FeedFetchResult] = {}
    for collector in collectors:
        if isinstance(collector, DiscoveredFeedCollector):
            feed_results.update(collector.feed_results())
    return feed_results


def _count_new_entries_by_feed_url(
    feed_results: dict[str, FeedFetchResult], candidates: Sequence[CandidateArticle]
) -> dict[str, int]:
    """取得に成功したフィードごとに、除外を通り抜けた候補の件数を数える（Issue #109）。

    取得に成功したフィードは 0 件でも結果に含める（「新着が無かった」ことを数える
    ため）。取得に失敗したフィードは含めない。`feed_url` を持たない候補
    （HN / GitHub Releases / arXiv / Brave 由来）はどのフィードにも数えない。
    """
    counts = {feed_url: 0 for feed_url, result in feed_results.items() if result.succeeded}
    for candidate in candidates:
        feed_url = candidate.feed_url
        if feed_url is not None and feed_url in counts:
            counts[feed_url] += 1
    return counts


def _discover_new_feeds_safely(session: Session, *, settings: Settings) -> None:
    """登録記事のドメインから新規巡回先を発見する（Issue #93）。

    1 ドメインの発見失敗は `discovery.discover_feeds` の内部で吸収されるが、
    ドメイン集計クエリ自体の失敗など想定外の例外まで含めて、この処理の失敗で
    巡回結果（既に enqueue 済みの候補）を失わせないよう、ここでも `_collect_all`
    と同じ「1 箇所の失敗が全体を止めない」方針で広く捕捉する。

    捕捉するだけでは足りないため、発見処理全体を savepoint で囲む。`discover_feeds`
    は `discovered_feeds` へ書き込むので、その flush が失敗するとセッションが
    「rollback 待ち」のまま残り、呼び出し元（`jobs.handlers._shared._run_sync`）の
    `session.commit()` が `PendingRollbackError` で落ちて、enqueue 済みの候補まで
    巻き添えで消える。savepoint を張っておけば、例外時に発見処理ぶんだけを巻き戻して
    セッションを使える状態へ戻せる。
    """
    try:
        with session.begin_nested():
            discover_feeds(session, user_id=settings.default_user_id, settings=settings)
    except Exception:
        logger.warning("collectors.service.feed_discovery_failed", exc_info=True)


def _build_default_collectors(
    session: Session, settings: Settings, feeds_config: FeedsConfig
) -> tuple[SourceCollector, ...]:
    """既定の巡回対象（RSS / 国内メディア / HN / GitHub Releases / arXiv / 発見済み / Brave）
    を組み立てる。
    """
    collectors: list[SourceCollector] = [
        RssCollector(feeds_config.rss, settings),
        JpMediaCollector(feeds_config.jp_media, settings),
        HackerNewsCollector(feeds_config=feeds_config, settings=settings),
        GitHubReleasesCollector(feeds_config=feeds_config, settings=settings),
        ArxivCollector(feeds_config.arxiv_categories, settings),
        DiscoveredFeedCollector(load_enabled_discovered_feeds(session), settings),
    ]

    # Brave は API キー未設定時、コレクター自身の `collect()` でも空リストを返す
    # （`BraveSearchCollector.collect` 参照）。それでもここで構築自体を省くのは、
    # クエリ生成のために DB から直近記事の topics/technologies を集める処理
    # （`_collect_interest_terms`）が、どうせ即座に捨てられる Brave 用にだけ
    # 余計な DB 問い合わせを発生させるのを避けるため（設計判断）。
    if settings.is_brave_search_enabled:
        topics, technologies = _collect_interest_terms(session)
        queries = build_search_queries(topics=topics, technologies=technologies)
        collectors.append(BraveSearchCollector(queries, settings))

    return tuple(collectors)


def _collect_interest_terms(session: Session) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Brave 検索クエリの元になる topics / technologies を直近の解析済み記事から集める。

    LLM は使わず、既存記事が解析時に確定させた `topics` / `technologies` を
    そのまま流用する（追加課金ゼロが本プロジェクトの制約のため）。件数は
    `INTEREST_SOURCE_ARTICLE_LIMIT` 件に抑え、古い記事まで無制限に遡らない。
    """
    stmt = (
        select(Article.topics, Article.technologies)
        .where(Article.analysis_status == JobStatus.COMPLETED.value)
        .order_by(Article.fetched_at.desc())
        .limit(INTEREST_SOURCE_ARTICLE_LIMIT)
    )

    topics: list[str] = []
    technologies: list[str] = []
    seen_topics: set[str] = set()
    seen_technologies: set[str] = set()
    for row_topics, row_technologies in session.execute(stmt):
        _extend_unique(topics, seen_topics, row_topics)
        _extend_unique(technologies, seen_technologies, row_technologies)
    return tuple(topics), tuple(technologies)


def _extend_unique(target: list[str], seen: set[str], values: Sequence[str]) -> None:
    """`values` のうち未登場の要素だけ、順序を保ったまま `target` へ追加する。"""
    for value in values:
        if value not in seen:
            seen.add(value)
            target.append(value)


def _collect_all(collectors: Sequence[SourceCollector]) -> tuple[CandidateArticle, ...]:
    """各コレクターの `collect()` を順に呼ぶ。1 つの失敗が他を巻き込まない。

    `CollectorError` に限らず想定外の例外も含めて捕捉する。1 コレクターの
    バグや外部 API の想定外の応答で巡回全体が止まると、正常な他の巡回先からも
    候補を1件も得られなくなるため。
    """
    candidates: list[CandidateArticle] = []
    for collector in collectors:
        try:
            candidates.extend(collector.collect())
        except Exception:
            logger.warning(
                "collectors.service.collector_failed collector=%s", collector.name, exc_info=True
            )
    return tuple(candidates)


def _filter_by_source_domain(
    candidates: Sequence[CandidateArticle], source_domain: str | None
) -> tuple[CandidateArticle, ...]:
    """`source_domain` が指定されていれば、ホスト一致（サブドメイン含む）だけ残す。

    文字列の部分一致で判定すると `evil-example.com` が `example.com` へ誤って
    一致してしまうため、必ず `urllib.parse.urlsplit` でホスト名を取り出してから
    比較する。
    """
    if source_domain is None:
        return tuple(candidates)

    normalized_domain = source_domain.lower()
    return tuple(
        candidate
        for candidate in candidates
        if _host_matches(urlsplit(candidate.url).hostname, normalized_domain)
    )


def _host_matches(hostname: str | None, domain: str) -> bool:
    """ホストが `domain` 自身、またはそのサブドメインかを判定する。"""
    if hostname is None:
        return False
    hostname = hostname.lower()
    return hostname == domain or hostname.endswith(f".{domain}")


def _dedupe_by_normalized_url(
    candidates: Sequence[CandidateArticle],
) -> tuple[CandidateArticle, ...]:
    """正規化 URL が重複する候補を、先に見つかったほうを残して1件に絞る。"""
    deduped: list[CandidateArticle] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = normalize_url(candidate.url)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return tuple(deduped)


def _exclude_existing_articles(
    session: Session, candidates: Sequence[CandidateArticle]
) -> tuple[CandidateArticle, ...]:
    """既に `articles` にある記事（正規化 URL 一致）を除外する。

    再フェッチしないことで無駄な HTTP 通信と LLM 呼び出しを避ける
    （`PROJECT_SPEC.md` §24 コスト管理）。
    """
    if not candidates:
        return ()

    normalized_urls = {normalize_url(candidate.url) for candidate in candidates}
    existing_urls = set(
        session.scalars(
            select(Article.canonical_url).where(Article.canonical_url.in_(normalized_urls))
        )
    )
    return tuple(
        candidate for candidate in candidates if normalize_url(candidate.url) not in existing_urls
    )


def _exclude_already_queued(
    session: Session, candidates: Sequence[CandidateArticle]
) -> tuple[CandidateArticle, ...]:
    """同じ URL の pending / 実行中 `fetch_article` ジョブがあれば除外する。

    巡回のたびに同じ候補を積み増すと、まだ処理されていないジョブが URL 単位で
    何重にも積まれてしまう（二重 enqueue の防止）。
    """
    if not candidates:
        return ()

    active_jobs = session.scalars(
        select(Job).where(
            Job.type == JobType.FETCH_ARTICLE.value,
            Job.status.in_(_ACTIVE_FETCH_ARTICLE_STATUSES),
        )
    )
    queued_urls = {
        normalize_url(url) for job in active_jobs if isinstance(url := job.payload.get("url"), str)
    }
    return tuple(
        candidate for candidate in candidates if normalize_url(candidate.url) not in queued_urls
    )


def _enqueue_fetch_jobs(session: Session, candidates: Sequence[CandidateArticle]) -> int:
    """絞り込み済みの候補ごとに `fetch_article` ジョブを積む。

    `registration_id` を持たせない。巡回由来のジョブにはユーザーの明示的な
    URL 登録行が無いため（`techradar.jobs.handlers.fetch_article` 側が
    `registration_id` の有無で経路を分ける）。代わりに `origin` で巡回由来の
    ジョブであることを判別できるようにする。
    """
    for candidate in candidates:
        enqueue(
            session,
            JobType.FETCH_ARTICLE,
            {"url": candidate.url, "origin": CRAWL_ORIGIN},
        )
    return len(candidates)
