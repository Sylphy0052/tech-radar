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
- `MAX_FEED_CANDIDATES_PER_DOMAIN`: 1 ドメインのトップページから試すフィード
  候補数の上限。トップページの HTML は最大 `fetch_max_response_bytes`（既定 5MB）
  まで許容されるため、`<link rel="alternate">` を大量に並べた HTML を踏むと、
  1 ドメインだけで「候補数 × `fetch_total_timeout_seconds`」の時間をジョブ
  ワーカーが占有されうる。上限がこれを抑える。

外部サイトの HTML が指す URL をそのまま巡回対象にはしない。候補はトップページと
同じホスト（またはそのサブドメイン）の https URL に限る。`<link rel="alternate">`
の `href` には任意の第三者 URL を書けるため、制限しないと「攻撃者が選んだ URL を
恒久的な巡回先として登録させる」ことができてしまう（内部アドレスへの到達自体は
`fetcher.ssrf.validate_url` が別途止めるが、外部サイトの選択までは止めない）。
`fetcher.url.resolve_canonical_url` が「別ホストの canonical は採用しない」と
しているのと同じ考え方。

ドメイン集計は `interest.service._load_interest_article_population` と共用しない
（モジュール docstring と `DiscoveredFeed` モデルの docstring を参照。要点は、
あちらは関心スコア計算用の母集団で `article_feedback` の補完・重み計算を含み
戻り値の型・責務が異なるため、無理に共用するとインターフェースが歪む）。
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

import feedparser
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.collectors.config import FeedEntryConfig
from techradar.collectors.rss import FeedFetchResult, RssCollector
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

# 1 ドメインのトップページから取得を試すフィード候補数の上限。
MAX_FEED_CANDIDATES_PER_DOMAIN = 5

# 発見済みフィードの死活監視（Issue #105）で許容する連続失敗回数の上限。これに
# 達すると status=DISABLED / enabled=False にして巡回対象から外す。1 回程度の
# 失敗はネットワークの一時的な不調でも起きうるため、単発の失敗では無効化しない。
MAX_CONSECUTIVE_FEED_FAILURES = 3

# 発見済みフィードの「記事を配信しない」無効化（Issue #108）で許容する連続
# エントリ 0 件回数の上限。失敗の閾値（`MAX_CONSECUTIVE_FEED_FAILURES` = 3）より
# 十分に大きく取る。巡回は UI の実行ボタンからの手動起動で実時間の間隔が
# 読めないため、更新頻度の低い（数回に 1 回しか新着が無い）フィードを
# 巻き込んで無効化しないよう、失敗より遥かに粘る値にしてある。
MAX_CONSECUTIVE_EMPTY_FETCHES = 10

# 発見済みフィードの「新着が出ない」無効化（Issue #109）で許容する連続回数の上限。
# ここで数えるのは「エントリは配信したが、絞り込み（freshness / 重複排除 / 既存記事
# 除外 / enqueue 済み除外）を通り抜けた新着が 1 件も無かった」回数。空配信の閾値
# （`MAX_CONSECUTIVE_EMPTY_FETCHES` = 10）よりさらに大きく取る。既出記事だけを返す
# こと自体は異常ではなく（更新の遅いフィード・巡回間隔が短い場合に普通に起きる）、
# 配信そのものが止まったフィードは空配信の側が先に無効化するため。
MAX_CONSECUTIVE_STALE_FETCHES = 20

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

    status_counts: Counter[str] = Counter()
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
        status_counts[status] += 1

    found_count = status_counts[DiscoveredFeedStatus.FOUND.value]
    # 常駐監視も CI も持たないため、事後にログだけで何が起きたか読めるようにしておく
    # （`collect_candidates` が完了時にサマリを出すのと同じ理由）。
    logger.info(
        "collectors.discovery.completed attempted=%d found=%d not_found=%d fetch_failed=%d",
        len(targets),
        found_count,
        status_counts[DiscoveredFeedStatus.NOT_FOUND.value],
        status_counts[DiscoveredFeedStatus.FETCH_FAILED.value],
    )
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


def record_feed_health(
    session: Session, feed_results: dict[str, FeedFetchResult], *, now: datetime | None = None
) -> None:
    """`DiscoveredFeedCollector` の巡回結果を `discovered_feeds` へ反映する（Issue #105, #108）。

    `feed_results` は `RssCollector.feed_results()`（`DiscoveredFeedCollector` が
    継承する）が返す「フィード URL → `FeedFetchResult`」の対応をそのまま渡す想定。
    `feeds.yaml` 由来の手動フィード（`RssCollector` / `JpMediaCollector`）は
    対象外にする。自動追加の枠（`MAX_DISCOVERED_FEEDS_TOTAL`）を消費しないため
    無効化する動機が無く、人が意図して置いたフィードを機械が黙って外す方が
    危ういため（呼び出し側の `collectors.service` が `DiscoveredFeedCollector`
    のときだけこの関数を呼ぶことで、対象を絞る）。

    取得・パースに成功したフィードは `consecutive_failures` を 0 へリセットし
    `last_succeeded_at` を更新する。そのうえでエントリ 0 件だったかどうかで
    `consecutive_empty_fetches` を扱う（Issue #108）。1 件でも配信できていれば
    0 へ戻し、0 件なら 1 増やして `MAX_CONSECUTIVE_EMPTY_FETCHES` に達したら
    `status=DISABLED` / `enabled=False` にする（記事を配信しないフィードが
    `MAX_DISCOVERED_FEEDS_TOTAL` の枠を専有し続けるのを防ぐ）。取得・パース自体に
    失敗したフィードは `consecutive_failures` を 1 増やし、
    `MAX_CONSECUTIVE_FEED_FAILURES` に達したら同様に無効化する。失敗の回は
    「記事が無かった」とは数えない（取得できていない以上、記事の有無は
    分からないため）ので `consecutive_empty_fetches` には一切触れない
    （増やしも、0 へ戻しもしない）。無効化の理由（失敗によるものか、空配信に
    よるものか）はログのイベント名で区別する。対象の行が見つからない URL
    （巡回中に行が消えた場合など）は黙って無視する。

    1 つの `feed_url` に複数の行が対応することを許す。一意なのは `domain` だけで
    `feed_url` に一意制約は無く、別々のドメインのトップページが同じフィード URL
    を指していれば行が並ぶ（`_extract_feed_link_candidates` はトップページと同じ
    ホストかそのサブドメインであることしか求めないため、`a.example.com` と
    `b.example.com` の両方が `https://example.com/feed.xml` を指す形は通る）。
    1 行だけ更新すると残りの行の成否が黙って捨てられるため、一致する行はすべて
    更新する。

    無効化すると、なぜ枠が空き・復活も拾えるかは `_available_slots` /
    `_already_found_domains` / `_cooldown_domains` の3関数が
    `status == FOUND` だけを見ているため。`DISABLED` にした時点で
    `_available_slots` の残り枠が増え、同時に `_already_found_domains` の
    対象から外れて `_cooldown_domains`（`status != FOUND` を対象にする）
    経由で `DISCOVERY_RETRY_COOLDOWN_DAYS` 後に再発見の対象へ戻る。
    この3関数は変更しない（新しい status は増やさない、Issue #108 の設計判断）。
    """
    resolved_now = now or datetime.now(UTC)
    for feed_url, result, row in _iter_live_feed_rows(
        session, feed_results, missing_event="feed_health_row_missing"
    ):
        if result.succeeded:
            row.consecutive_failures = 0
            row.last_succeeded_at = resolved_now
            _apply_empty_fetch_result(row, result, feed_url=feed_url)
            continue

        row.consecutive_failures += 1
        if row.consecutive_failures >= MAX_CONSECUTIVE_FEED_FAILURES:
            _disable_feed(row)
            logger.info(
                "collectors.discovery.feed_disabled domain=%s feed_url=%s consecutive_failures=%d",
                row.domain,
                feed_url,
                row.consecutive_failures,
            )
    _flush_feed_updates(session)


def _apply_empty_fetch_result(
    row: DiscoveredFeed, result: FeedFetchResult, *, feed_url: str
) -> None:
    """取得・パースに成功した1行へ、エントリ 0 件の連続回数を反映する（Issue #108）。

    `record_feed_health` から、`result.succeeded` が True の行に対してのみ呼ぶ
    （呼び出し元で分岐済み）。
    """
    if result.entry_count > 0:
        row.consecutive_empty_fetches = 0
        return

    row.consecutive_empty_fetches += 1
    if row.consecutive_empty_fetches >= MAX_CONSECUTIVE_EMPTY_FETCHES:
        _disable_feed(row)
        logger.info(
            "collectors.discovery.feed_disabled_empty "
            "domain=%s feed_url=%s consecutive_empty_fetches=%d",
            row.domain,
            feed_url,
            row.consecutive_empty_fetches,
        )


@dataclass(frozen=True)
class FeedNoveltyResult:
    """1 フィードぶんの「新着が出たか」の巡回結果（Issue #109）。

    `succeeded` と `entry_count` は `FeedFetchResult` と同じ意味（取得・パースの
    成否と、絞り込み前の候補記事の件数）。`new_article_count` はそこへ
    `collectors.service.collect_candidates` の絞り込み（`filter_recent` /
    `_dedupe_by_normalized_url` / `_exclude_existing_articles` /
    `_exclude_already_queued`）を通した後に残った件数、つまり実際に新着として
    積まれた件数を表す。

    `FeedFetchResult` を拡張せず別の型にしてあるのは、値が確定する時点が違う
    ため。`FeedFetchResult` はコレクターが巡回した時点で確定するが、絞り込みは
    その後段（DB を見る処理）で行われる。コレクターは DB を知らないままにする
    という Issue #105 の設計判断を崩さないよう、絞り込み後の件数は
    コレクターの結果型には持たせない。
    """

    succeeded: bool
    entry_count: int
    new_article_count: int


def record_feed_novelty(session: Session, feed_results: dict[str, FeedNoveltyResult]) -> None:
    """新着が出ないフィードを検出し `discovered_feeds` へ反映する（Issue #109）。

    `record_feed_health`（#105 の連続失敗・#108 の連続空配信）とは列もイベント名も
    分ける。あちらが見る `FeedFetchResult.entry_count` は絞り込み前の件数のため、
    「毎回同じ既出記事だけを返すフィード」は `entry_count > 0` となって捕まらず、
    実質的な新着ゼロのまま `MAX_DISCOVERED_FEEDS_TOTAL` の枠を専有し続ける。
    ここではその分を `consecutive_stale_fetches` に別の閾値
    （`MAX_CONSECUTIVE_STALE_FETCHES`）で数える。

    数えるのは「取得・パースに成功し、かつエントリを 1 件以上配信した」回だけに
    限る。判定を #105 / #108 と重ねないため:

    - 取得・パースに失敗した回は新着の有無が分からない。カウンタへ触れない
      （増やしも 0 へ戻しもしない）。失敗が続けば `consecutive_failures` が先に
      閾値へ達する
    - エントリ自体が 0 件だった回は `consecutive_empty_fetches` の担当
      （#108）。同じ事象を二重に数えないよう、ここでは触れない

    そのうえで、絞り込み後に新着が 1 件でも残っていれば 0 へ戻し、0 件なら 1 増やして
    `MAX_CONSECUTIVE_STALE_FETCHES` に達した時点で `status=DISABLED` /
    `enabled=False` にする。枠が空く仕組み・再発見で復活する仕組み・同じ
    `feed_url` を持つ行をすべて更新する理由・無効化済みの行へ触れない理由は
    `record_feed_health` の docstring と `_iter_live_feed_rows` を参照。

    「一定期間にわたって新着が出ていない」の「期間」は連続した巡回の回数で数える。
    巡回は UI の実行ボタンからの手動起動で実時間の間隔が読めないため、壁時計の
    経過時間では判定できない（常駐スケジューラを置かないというプロジェクトの制約）。
    そのため `record_feed_health` と違い、この関数は時刻を受け取らない。
    """
    for feed_url, result, row in _iter_live_feed_rows(
        session, feed_results, missing_event="feed_novelty_row_missing"
    ):
        if not result.succeeded or result.entry_count <= 0:
            continue

        if result.new_article_count > 0:
            row.consecutive_stale_fetches = 0
            continue

        row.consecutive_stale_fetches += 1
        if row.consecutive_stale_fetches >= MAX_CONSECUTIVE_STALE_FETCHES:
            _disable_feed(row)
            logger.info(
                "collectors.discovery.feed_disabled_stale "
                "domain=%s feed_url=%s consecutive_stale_fetches=%d",
                row.domain,
                feed_url,
                row.consecutive_stale_fetches,
            )
    _flush_feed_updates(session)


def _disable_feed(row: DiscoveredFeed) -> None:
    """行を巡回対象から外す（Issue #105, #108, #109 の無効化に共通）。

    無効化の理由（連続失敗・連続空配信・連続新着ゼロ）はログのイベント名で
    区別するため、ログは呼び出し側に置く。`status` と `enabled` は必ず対で
    倒す（`load_enabled_discovered_feeds` は両方を見る）。
    """
    row.status = DiscoveredFeedStatus.DISABLED.value
    row.enabled = False


def _flush_feed_updates(session: Session) -> None:
    """巡回結果の反映を flush する（Issue #105, #108, #109 に共通）。

    呼び出し側（`collectors.service`）が savepoint（`begin_nested`）で囲む前提
    だが、テストや直接呼び出しでも変更が即座に読めるよう明示的に flush する
    （`_upsert_discovered_feed` と同じ流儀）。
    """
    session.flush()


def _iter_live_feed_rows[ResultT](
    session: Session, feed_results: Mapping[str, ResultT], *, missing_event: str
) -> Iterator[tuple[str, ResultT, DiscoveredFeed]]:
    """巡回結果と、反映先の `discovered_feeds` の行の組を順に返す（Issue #105, #108, #109）。

    `record_feed_health` と `record_feed_novelty` に共通する走査。結果の型は
    どちらでもよいので、行の絞り込みだけを担い、カウンタの扱いは呼び出し側に残す。

    対象の行が見つからない `feed_url`（巡回中に行が消えた場合など）は黙って飛ばす。
    反映すべき対象が無いだけなので他の URL の反映は続けるが、この分岐へ頻繁に
    入るのは巡回対象と `discovered_feeds` が乖離しているということなので、痕跡は
    残しておく（常駐監視も CI も持たないため、事後にログだけで追えるようにする）。
    どちらの経路で起きたかを区別できるよう、イベント名は `missing_event` で受ける。

    既に無効化した行も飛ばす。無効化済みの行は巡回対象ではない
    （`load_enabled_discovered_feeds` は status=FOUND かつ enabled のみを返す）。
    ここへ来るのは、同じ feed_url を持つ別ドメインの行がまだ生きていて、その巡回
    結果が相乗りしてくる場合だけ。取得していない行の結果を数える意味は無く、
    放置するとカウンタが際限なく増え、無効化のログも巡回のたびに出続ける。
    復活は再発見（`_apply_discovery_result`）が担う。
    """
    rows_by_feed_url = _discovered_feeds_by_url(session, feed_results.keys())
    for feed_url, result in feed_results.items():
        rows = rows_by_feed_url.get(feed_url, ())
        if not rows:
            logger.debug("collectors.discovery.%s feed_url=%s", missing_event, feed_url)
            continue
        for row in rows:
            if row.status == DiscoveredFeedStatus.DISABLED.value:
                continue
            yield feed_url, result, row


def _discovered_feeds_by_url(
    session: Session, feed_urls: Iterable[str]
) -> dict[str, list[DiscoveredFeed]]:
    """`feed_url` をキーに `discovered_feeds` の行をまとめて引く（Issue #105）。

    URL ごとに SELECT を出さず 1 クエリで済ませる。`feed_url` は一意ではないため
    値は行のリストになる（`record_feed_health` docstring 参照）。
    """
    urls = list(feed_urls)
    if not urls:
        return {}
    rows = session.scalars(select(DiscoveredFeed).where(DiscoveredFeed.feed_url.in_(urls))).all()
    grouped: dict[str, list[DiscoveredFeed]] = {}
    for row in rows:
        if row.feed_url is None:
            continue
        grouped.setdefault(row.feed_url, []).append(row)
    return grouped


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
        if _validate_feed(candidate_url, settings=settings):
            return DiscoveredFeedStatus.FOUND.value, candidate_url

    return DiscoveredFeedStatus.NOT_FOUND.value, None


def _extract_feed_link_candidates(html: str, base_url: str) -> tuple[str, ...]:
    """HTML から取得を試す価値のあるフィード候補 URL を集める。

    `<link rel="alternate" type="application/(rss|atom)+xml">` の href のうち、
    次をすべて満たすものだけを、HTML に現れた順で最大
    `MAX_FEED_CANDIDATES_PER_DOMAIN` 件返す。

    - `base_url`（リダイレクト追跡後の `final_url`）と同じホスト、またはその
      サブドメインであること（モジュール docstring 参照）
    - https であること（`FeedEntryConfig` の `_require_https` と整合させる）
    - 既に候補へ入っていないこと（同じ href を複数のタグに書くページがあり、
      そのままだと同一 URL を二度取得してしまう）

    `rel` は bs4 のビルダーによって複数値（list）にも単一値（str）にもなりうるため、
    どちらでも判定できるようにする。`type` は `Content-Type` ヘッダと同じ書式を
    取りうるので、`fetcher.http._check_content_type` と同様にパラメータを落として
    小文字化してから比較する（`type="application/rss+xml; charset=utf-8"` のような
    表記を取りこぼさないため）。
    """
    base_host = _hostname_of(base_url)
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
        if "alternate" not in {value.lower() for value in rel_values}:
            continue

        type_attr = tag.get("type")
        if not isinstance(type_attr, str):
            continue
        if type_attr.split(";", 1)[0].strip().lower() not in _FEED_LINK_TYPES:
            continue

        href = tag.get("href")
        if not isinstance(href, str) or not href:
            continue

        candidate_url = urljoin(base_url, href)
        if not candidate_url.startswith("https://"):
            continue
        if not _is_same_site(candidate_url, base_host):
            continue
        if candidate_url in candidates:
            continue
        candidates.append(candidate_url)
        if len(candidates) >= MAX_FEED_CANDIDATES_PER_DOMAIN:
            break
    return tuple(candidates)


def _hostname_of(url: str) -> str:
    """URL のホスト名を小文字で返す（取り出せなければ空文字）。"""
    return (urlsplit(url).hostname or "").lower()


def _is_same_site(candidate_url: str, base_host: str) -> bool:
    """候補 URL がトップページと同じホスト、またはそのサブドメインかどうかを返す。

    公開接尾辞リスト（`example.co.jp` のような多段 TLD）は見ない。判定を
    「トップページのホストと一致するか、その配下か」に閉じているため、
    上位ドメインを跨いで広がることはない。`base_host` が取れないときは
    比較のしようがないので採用しない（安全側へ倒す）。
    """
    if not base_host:
        return False
    host = _hostname_of(candidate_url)
    return host == base_host or host.endswith(f".{base_host}")


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
    """発見結果を既存の `DiscoveredFeed` 行へ反映する（新規作成・更新の両方から使う共通処理）。

    `status=FOUND` になったときは `consecutive_failures` と
    `consecutive_empty_fetches` の両方を 0 へ戻す（Issue #105, #108）。連続失敗・
    連続空配信で `DISABLED` にした行は `DISCOVERY_RETRY_COOLDOWN_DAYS` 後に再発見の
    対象へ戻るが、回数を持ち越したままだと復活直後の 1 回の失敗・空配信で
    `MAX_CONSECUTIVE_FEED_FAILURES` / `MAX_CONSECUTIVE_EMPTY_FETCHES` に達して
    即座に無効化されてしまい、「一時的な不調では無効化しない」という前提が
    復活後だけ成り立たなくなるため。
    """
    row.feed_url = feed_url
    row.status = status
    row.article_count = article_count
    row.last_attempted_at = now
    row.enabled = status == DiscoveredFeedStatus.FOUND.value
    if status == DiscoveredFeedStatus.FOUND.value:
        row.consecutive_failures = 0
        row.consecutive_empty_fetches = 0


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
