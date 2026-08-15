"""登録記事のドメインから巡回先を自動発見する（Issue #93）。

固定の `config/feeds.yaml` だけでは、ユーザーが実際に登録している記事のサイトが
巡回先へ反映されない。ユーザーの登録記事（`user_articles`）をドメイン別に集計し、
上位ドメインのトップページから RSS/Atom フィードを探して `discovered_feeds` へ
記録する。発見できたフィードは即座に `enabled=True` にする（確認 UI はスコープ外、
Issue #93 ヒアリングでの決定）。

安全弁（すべてコード定数、`feeds.yaml` は人間がキュレーションする巡回先の宣言用
ファイルであり、アルゴリズムのパラメータを書く場所ではないため）:
- `MAX_DISCOVERY_DOMAINS_PER_RUN`: 1 回の巡回で新規発見を試みるドメイン数の上限。
  発見処理はドメインごとに HTML/フィードの取得を伴うため、巡回のたびに際限なく
  HTTP を出さないための上限。
- `MAX_DISCOVERED_FEEDS_TOTAL`: 自動追加できる `discovered_feeds`（status=FOUND）
  の総数上限。無制限に増やすと `feeds.yaml` の手動キュレーションと拮抗し、
  巡回そのものの所要時間が際限なく伸びる。
- `DISCOVERY_RETRY_COOLDOWN_DAYS`: 発見に失敗した（NOT_FOUND/FETCH_FAILED）
  ドメインを再試行するまでの間隔。短すぎると、フィードを持たない・一時的に
  落ちているドメインへ毎回 HTTP を出し続けてしまう。

ドメイン集計は `interest.service._load_interest_article_population` と共用しない
（モジュール docstring と `DiscoveredFeed` モデルの docstring を参照。要点は、
あちらは関心スコア計算用の母集団で `article_feedback` の補完・重み計算を含み
戻り値の型・責務が異なるため、無理に共用するとインターフェースが歪む）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.collectors.config import FeedEntryConfig
from techradar.collectors.rss import RssCollector
from techradar.config import Settings
from techradar.db.enums import ArticleOrigin, DiscoveredFeedStatus
from techradar.db.errors import is_unique_violation
from techradar.db.models import Article, DiscoveredFeed, UserArticle
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import FEED_CONTENT_TYPES, HTML_CONTENT_TYPES, fetch_resource

logger = logging.getLogger(__name__)

# 1 回の巡回で新規発見を試みるドメイン数の上限。
MAX_DISCOVERY_DOMAINS_PER_RUN = 5

# 自動追加できる discovered_feeds（status=FOUND）の総数上限。
MAX_DISCOVERED_FEEDS_TOTAL = 20

# 発見に失敗したドメインを再試行するまでの間隔（日数）。
DISCOVERY_RETRY_COOLDOWN_DAYS = 30

# ドメイン集計の対象にする UserArticle.origin。「実際に登録した記事のサイト」を
# 見たいだけなので、関心スコア計算用の5経路のうち read_full/clicked は含めない
# （どちらも本文閲覧・クリックの副産物で、ユーザーが能動的に選んだ登録ではないため）。
_POPULATION_ORIGIN_VALUES = (
    ArticleOrigin.MANUAL.value,
    ArticleOrigin.GOOD.value,
    ArticleOrigin.SAVED.value,
)

# <link rel="alternate"> のフィード候補と判定する type 属性。
_FEED_LINK_TYPES = ("application/rss+xml", "application/atom+xml")


@dataclass(frozen=True)
class DomainCount:
    """ドメイン別の登録記事件数。"""

    domain: str
    article_count: int


def aggregate_domain_counts(session: Session, user_id: uuid.UUID) -> tuple[DomainCount, ...]:
    """ユーザーの登録記事（manual/good/saved）をドメイン別に集計し、件数降順で返す。

    同件数のドメインが並ぶ順序を安定させるため、件数降順の次にドメイン名の
    昇順で並べる（テストや再実行のたびに順序が揺れないようにするため）。
    """
    count_column = func.count().label("article_count")
    stmt = (
        select(Article.source_domain, count_column)
        .select_from(UserArticle)
        .join(Article, Article.id == UserArticle.article_id)
        .where(
            UserArticle.user_id == user_id,
            UserArticle.origin.in_(_POPULATION_ORIGIN_VALUES),
            Article.source_domain != "",
        )
        .group_by(Article.source_domain)
        .order_by(count_column.desc(), Article.source_domain)
    )
    return tuple(
        DomainCount(domain=domain, article_count=count) for domain, count in session.execute(stmt)
    )


def select_discovery_targets(
    ranked_domains: Sequence[DomainCount], *, excluded: set[str], max_domains: int
) -> tuple[DomainCount, ...]:
    """`ranked_domains` から `excluded` を除いた上位 `max_domains` 件を返す。

    DB/HTTP に触れない純粋関数として切り出し、上限・除外ロジックだけを
    単独でテストできるようにする。
    """
    candidates = [
        domain_count for domain_count in ranked_domains if domain_count.domain not in excluded
    ]
    return tuple(candidates[:max_domains])


def discover_feeds(
    session: Session, *, user_id: uuid.UUID, settings: Settings, now: datetime | None = None
) -> int:
    """登録記事のドメインから新規巡回先を発見し `discovered_feeds` へ反映する。

    見つかった（status=FOUND として書き込んだ）件数を返す。1 ドメインの発見で
    想定外の例外が起きても、ここで捕捉して `FETCH_FAILED` として記録し、他の
    ドメインの発見は続ける（`collectors.service._collect_all` と同じ「1 つの
    失敗が他を巻き込まない」方針）。呼び出し側（`collectors.service.collect_candidates`）
    は、この関数自体が想定外の例外を送出した場合に備えてさらに広く捕捉する。
    """
    resolved_now = now or datetime.now(UTC)

    available = _available_slots(session)
    if available <= 0:
        # 上限に達している間は新規ドメインへの発見処理自体を試みない
        # （無駄な HTTP を出さない）。
        return 0

    ranked = aggregate_domain_counts(session, user_id)
    if not ranked:
        return 0

    excluded = _already_found_domains(session) | _cooldown_domains(session, resolved_now)
    max_domains = min(MAX_DISCOVERY_DOMAINS_PER_RUN, available)
    targets = select_discovery_targets(ranked, excluded=excluded, max_domains=max_domains)

    found_count = 0
    for target in targets:
        try:
            status, feed_url = _discover_feed_url(target.domain, settings=settings)
        except Exception:
            logger.warning(
                "collectors.discovery.domain_failed domain=%s", target.domain, exc_info=True
            )
            status, feed_url = DiscoveredFeedStatus.FETCH_FAILED.value, None
        _upsert_discovered_feed(
            session,
            domain=target.domain,
            feed_url=feed_url,
            status=status,
            article_count=target.article_count,
            now=resolved_now,
        )
        if status == DiscoveredFeedStatus.FOUND.value:
            found_count += 1
    return found_count


def _already_found_domains(session: Session) -> set[str]:
    """既に `status=FOUND` のドメイン（再試行しない）を返す。"""
    stmt = select(DiscoveredFeed.domain).where(
        DiscoveredFeed.status == DiscoveredFeedStatus.FOUND.value
    )
    return set(session.scalars(stmt))


def _cooldown_domains(session: Session, now: datetime) -> set[str]:
    """再試行のクールダウン期間内にある（NOT_FOUND/FETCH_FAILED の）ドメインを返す。"""
    cutoff = now - timedelta(days=DISCOVERY_RETRY_COOLDOWN_DAYS)
    stmt = select(DiscoveredFeed.domain).where(
        DiscoveredFeed.status != DiscoveredFeedStatus.FOUND.value,
        DiscoveredFeed.last_attempted_at >= cutoff,
    )
    return set(session.scalars(stmt))


def _available_slots(session: Session) -> int:
    """自動追加総数上限までの残り枠を返す。"""
    found_count = session.scalar(
        select(func.count())
        .select_from(DiscoveredFeed)
        .where(DiscoveredFeed.status == DiscoveredFeedStatus.FOUND.value)
    )
    return max(0, MAX_DISCOVERED_FEEDS_TOTAL - (found_count or 0))


def _discover_feed_url(domain: str, *, settings: Settings) -> tuple[str, str | None]:
    """`domain` のトップページからフィード URL を発見する。

    ドメインのトップページ取得は `https://{domain}/` のみを試す（http への
    フォールバックはしない。フィード配信元も含め、このプロジェクトが新規に
    信頼する巡回先は https 限定にするという既存方針——`FeedEntryConfig` の
    `_require_https` バリデータ——に合わせるため）。

    返り値は `(DiscoveredFeedStatus の値, feed_url)`。
    """
    homepage_url = f"https://{domain}/"
    try:
        page = fetch_resource(
            homepage_url, allowed_content_types=HTML_CONTENT_TYPES, settings=settings
        )
    except FetchError as exc:
        logger.warning("collectors.discovery.homepage_fetch_failed domain=%s (%s)", domain, exc)
        return DiscoveredFeedStatus.FETCH_FAILED.value, None

    for candidate_url in _extract_feed_link_candidates(page.text, page.final_url):
        if not candidate_url.startswith("https://"):
            # http 候補は `FeedEntryConfig` の https 限定制約と整合させるため採用しない。
            continue
        if _validate_feed(candidate_url, settings=settings):
            return DiscoveredFeedStatus.FOUND.value, candidate_url

    return DiscoveredFeedStatus.NOT_FOUND.value, None


def _extract_feed_link_candidates(html: str, base_url: str) -> tuple[str, ...]:
    """HTML から `<link rel="alternate" type="application/(rss|atom)+xml">` の href を集める。

    `rel` は bs4 のビルダーによって複数値（list）にも単一値（str）にもなりうるため、
    どちらでも判定できるようにする。相対 URL は `base_url`（リダイレクト追跡後の
    `final_url`）を基準に絶対化する。
    """
    soup = BeautifulSoup(html, "lxml")
    candidates: list[str] = []
    for tag in soup.find_all("link"):
        rel = tag.get("rel")
        if isinstance(rel, list):
            rel_values = rel
        elif isinstance(rel, str):
            rel_values = [rel]
        else:
            rel_values = []
        if "alternate" not in rel_values:
            continue

        type_attr = tag.get("type")
        if type_attr not in _FEED_LINK_TYPES:
            continue

        href = tag.get("href")
        if not isinstance(href, str) or not href:
            continue
        candidates.append(urljoin(base_url, href))
    return tuple(candidates)


def _validate_feed(feed_url: str, *, settings: Settings) -> bool:
    """候補 URL を実際に取得・パースし、巡回できるフィードかどうかを確かめる。

    `type` 属性を信じるだけだと、リンク切れ・空フィード・実際には HTML を返す
    誤設定のページを素通りしてしまう。既存の `RssCollector._collect_feed` と
    同じ `feedparser.parse` + `bozo` 判定を通しておくことで、「発見はできたが
    実際には巡回できないフィード」が紛れ込まないようにする。
    """

    try:
        resource = fetch_resource(
            feed_url, allowed_content_types=FEED_CONTENT_TYPES, settings=settings
        )
    except FetchError as exc:
        logger.warning(
            "collectors.discovery.feed_validation_failed feed_url=%s (%s)", feed_url, exc
        )
        return False

    parsed = feedparser.parse(resource.text)
    return not (parsed.bozo and not parsed.entries)


def _upsert_discovered_feed(
    session: Session,
    *,
    domain: str,
    feed_url: str | None,
    status: str,
    article_count: int,
    now: datetime,
) -> None:
    """`domain` の行を作成、または既存行を新しい発見結果で上書きする。

    単一ユーザー・ローカル実行のため実際に競合が起きることはほぼ無いが、
    `interest.service` の upsert（`db.errors.is_unique_violation` を使う流儀）に
    合わせておく。
    """
    existing = session.scalars(
        select(DiscoveredFeed).where(DiscoveredFeed.domain == domain)
    ).one_or_none()
    if existing is not None:
        _apply_discovery_result(
            existing, feed_url=feed_url, status=status, article_count=article_count, now=now
        )
        session.flush()
        return

    row = DiscoveredFeed(
        domain=domain,
        feed_url=feed_url,
        status=status,
        article_count=article_count,
        last_attempted_at=now,
        enabled=(status == DiscoveredFeedStatus.FOUND.value),
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        # 同時実行で既に他方が挿入済み。既存行を読み直して更新する。
        existing = session.scalars(
            select(DiscoveredFeed).where(DiscoveredFeed.domain == domain)
        ).one_or_none()
        if existing is None:
            logger.error("collectors.discovery.upsert_race_missing domain=%s", domain)
            raise
        _apply_discovery_result(
            existing, feed_url=feed_url, status=status, article_count=article_count, now=now
        )
        session.flush()


def _apply_discovery_result(
    row: DiscoveredFeed, *, feed_url: str | None, status: str, article_count: int, now: datetime
) -> None:
    """発見結果を既存の `DiscoveredFeed` 行へ反映する（新規作成・更新の両方から使う共通処理）。"""
    row.feed_url = feed_url
    row.status = status
    row.article_count = article_count
    row.last_attempted_at = now
    row.enabled = status == DiscoveredFeedStatus.FOUND.value


def load_enabled_discovered_feeds(session: Session) -> tuple[FeedEntryConfig, ...]:
    """巡回対象にする発見済みフィードを読み込み `FeedEntryConfig` へ変換する。

    `status=FOUND` かつ `enabled=True` の行だけを対象にする。
    """
    stmt = select(DiscoveredFeed.domain, DiscoveredFeed.feed_url).where(
        DiscoveredFeed.status == DiscoveredFeedStatus.FOUND.value,
        DiscoveredFeed.enabled.is_(True),
    )
    configs: list[FeedEntryConfig] = []
    for domain, feed_url in session.execute(stmt):
        if not feed_url:
            # 理論上ここには来ない（FOUND なら常に feed_url を持つ）が、
            # 過去のデータ不整合に備えて防御的にスキップする。
            continue
        configs.append(FeedEntryConfig(name=domain, url=feed_url))
    return tuple(configs)


class DiscoveredFeedCollector(RssCollector):
    """自動発見した巡回対象（`discovered_feeds`、Issue #93）を巡回するコレクター。

    巡回対象は `load_enabled_discovered_feeds` が読み込む `FeedEntryConfig` の列。
    パース・変換ロジックは `RssCollector` のものをそのまま使う
    （`jp_media.JpMediaCollector` と同じサブクラスパターン）。`name` を分ける
    ことでログの発生源を区別する。
    """

    name: str = "discovered_feeds"
