"""新着が出ないフィードの検出を検証する（Issue #109）。

Issue #108 の `consecutive_empty_fetches` は `FeedFetchResult.entry_count`、つまり
freshness フィルタ・重複排除・既存記事除外・enqueue 済み除外のいずれも通す前の
件数を見ている。そのため「毎回同じ既出記事だけを返すフィード」は
`entry_count > 0` になり、実質的に新着ゼロでも枠（`MAX_DISCOVERED_FEEDS_TOTAL`）
を専有し続ける。

ここでは、絞り込み後に残った件数（= 新着件数）を別の列
`DiscoveredFeed.consecutive_stale_fetches` へ別の閾値
（`MAX_CONSECUTIVE_STALE_FETCHES`）で反映する `record_feed_novelty` を検証する。
`record_feed_health`（#105 の失敗・#108 の空配信）とは列もイベント名も分ける。

「一定期間にわたって新着が出ていない」の「期間」は、連続した巡回の回数で数える。
巡回は UI の実行ボタンからの手動起動で実時間の間隔が読めず（常駐スケジューラを
置かないというプロジェクトの制約）、壁時計の経過時間では判定できないため。
このモジュールのテストも壁時計に依存せず、時刻はすべて固定値 `NOW` を使う。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from techradar.collectors import discovery as discovery_module
from techradar.collectors.discovery import (
    MAX_CONSECUTIVE_EMPTY_FETCHES,
    MAX_CONSECUTIVE_STALE_FETCHES,
    FeedNoveltyResult,
    _available_slots,
    record_feed_novelty,
)
from techradar.db.enums import DiscoveredFeedStatus
from techradar.db.models import DiscoveredFeed

NOW = datetime(2026, 8, 16, tzinfo=UTC)


def make_discovered_feed(
    session: Session,
    *,
    domain: str,
    feed_url: str,
    status: DiscoveredFeedStatus = DiscoveredFeedStatus.FOUND,
    enabled: bool = True,
    consecutive_stale_fetches: int = 0,
    consecutive_empty_fetches: int = 0,
    consecutive_failures: int = 0,
) -> DiscoveredFeed:
    """`discovered_feeds` の1行を作る（`test_collectors_discovery.py` と同じ流儀）。

    新着の検出だけを見たいので、既定は「発見済み・有効・カウンタはすべて 0」に
    してある。
    """
    row = DiscoveredFeed(
        domain=domain,
        feed_url=feed_url,
        status=status.value,
        article_count=1,
        last_attempted_at=NOW,
        enabled=enabled,
        consecutive_failures=consecutive_failures,
        last_succeeded_at=NOW,
        consecutive_empty_fetches=consecutive_empty_fetches,
        consecutive_stale_fetches=consecutive_stale_fetches,
    )
    session.add(row)
    session.flush()
    return row


def stale(entry_count: int = 5) -> FeedNoveltyResult:
    """取得・パースには成功したが、絞り込み後に新着が1件も残らなかった巡回結果。

    `entry_count` を既定で 5 にしてあるのは、Issue #109 が扱うのが
    「エントリは返すが、すべて既出だった」フィードだからである（エントリ自体が
    0 件なら #108 の `consecutive_empty_fetches` が先に無効化する）。
    """
    return FeedNoveltyResult(succeeded=True, entry_count=entry_count, new_article_count=0)


def fresh(new_article_count: int = 1, *, entry_count: int = 10) -> FeedNoveltyResult:
    """絞り込みを通り抜けた新着が残った巡回結果。"""
    return FeedNoveltyResult(
        succeeded=True, entry_count=entry_count, new_article_count=new_article_count
    )


class TestRecordFeedNoveltyDetectsStaleFeeds:
    """受入基準: 毎回同じ既出記事だけを返すフィードが、続いたら無効化され枠が空く。"""

    def test_counts_a_fetch_with_entries_but_no_new_articles_as_stale(
        self, db_session: Session
    ) -> None:
        # Arrange — エントリは返すが、絞り込み後に残った新着は 0 件
        feed_url = "https://stale.example.com/feed.xml"
        row = make_discovered_feed(db_session, domain="stale.example.com", feed_url=feed_url)

        # Act
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert — #108 の空配信カウンタは動かない（エントリ自体はあるため）
        db_session.refresh(row)
        assert row.consecutive_stale_fetches == 1
        assert row.consecutive_empty_fetches == 0
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.enabled is True

    def test_disables_after_reaching_the_consecutive_stale_threshold(
        self, db_session: Session
    ) -> None:
        """境界値: 閾値ちょうどに達した回で無効化する。"""
        # Arrange — 閾値の1回手前まで積んでおく
        feed_url = "https://frozen.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="frozen.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 1,
        )

        # Act — 閾値目の巡回
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert
        db_session.refresh(row)
        assert row.consecutive_stale_fetches == MAX_CONSECUTIVE_STALE_FETCHES
        assert row.status == DiscoveredFeedStatus.DISABLED.value
        assert row.enabled is False

    def test_does_not_disable_one_fetch_before_the_threshold(self, db_session: Session) -> None:
        """境界値: 閾値の1回手前では無効化しない（境目の下側）。"""
        # Arrange — 2回手前まで積んでおく
        feed_url = "https://almost.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="almost.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 2,
        )

        # Act — 閾値の1回手前まで進む
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert
        db_session.refresh(row)
        assert row.consecutive_stale_fetches == MAX_CONSECUTIVE_STALE_FETCHES - 1
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.enabled is True

    def test_frees_a_slot_after_disabling_a_stale_feed(self, db_session: Session) -> None:
        """受入基準: 無効化した時点で自動追加の残り枠が増える（#105 / #108 と同じ性質）。"""
        # Arrange
        feed_url = "https://toremove-stale.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="toremove-stale.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 1,
        )
        slots_before = _available_slots(db_session)

        # Act
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert
        db_session.refresh(row)
        assert row.status == DiscoveredFeedStatus.DISABLED.value
        assert _available_slots(db_session) == slots_before + 1

    def test_disables_every_row_sharing_the_same_feed_url(self, db_session: Session) -> None:
        """受入基準: 同じ `feed_url` を持つ行が複数あれば、そのすべてへ反映する。

        一意なのは `domain` だけで `feed_url` に一意制約は無い
        （`record_feed_health` docstring 参照）。1行だけ更新すると残りの行の
        判定が黙って狂う。
        """
        # Arrange — 同じ feed_url を指す 2 ドメイン。どちらも次の巡回で閾値へ届く
        feed_url = "https://shared-stale.example.com/feed.xml"
        rows = [
            make_discovered_feed(
                db_session,
                domain=domain,
                feed_url=feed_url,
                consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 1,
            )
            for domain in ("shared-stale.example.com", "blog.shared-stale.example.com")
        ]

        # Act
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert
        for row in rows:
            db_session.refresh(row)
            assert row.status == DiscoveredFeedStatus.DISABLED.value
            assert row.enabled is False
            assert row.consecutive_stale_fetches == MAX_CONSECUTIVE_STALE_FETCHES

    def test_ignores_a_feed_url_with_no_matching_row(self, db_session: Session) -> None:
        """巡回中に行が消えた場合など、対象の行が無い URL は黙って無視する。"""
        # Act / Assert — 例外を送出しない
        record_feed_novelty(db_session, {"https://gone.example.com/feed.xml": stale()})


class TestRecordFeedNoveltyKeepsLiveFeeds:
    """受入基準: 新着があるフィードは、除外で件数が減っただけでは無効化されない。"""

    def test_resets_the_counter_when_one_new_article_survives_the_filters(
        self, db_session: Session
    ) -> None:
        # Arrange — 閾値の1回手前まで積んだ状態で、10件中1件だけが新着
        feed_url = "https://alive.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="alive.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 1,
        )

        # Act
        record_feed_novelty(db_session, {feed_url: fresh(new_article_count=1, entry_count=10)})

        # Assert
        db_session.refresh(row)
        assert row.consecutive_stale_fetches == 0
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.enabled is True

    def test_a_failed_fetch_does_not_count_as_a_stale_fetch(self, db_session: Session) -> None:
        """取得・パースに失敗した回は新着の有無を判定できない。カウンタに触れない。

        増やしも 0 へ戻しもしない（#108 が失敗時に `consecutive_empty_fetches` へ
        触れないのと同じ扱い）。失敗が続けば #105 の `consecutive_failures` が
        先に閾値へ達して無効化する。
        """
        # Arrange
        feed_url = "https://flaky-stale.example.com/feed.xml"
        row = make_discovered_feed(
            db_session,
            domain="flaky-stale.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=5,
        )

        # Act
        record_feed_novelty(
            db_session,
            {feed_url: FeedNoveltyResult(succeeded=False, entry_count=0, new_article_count=0)},
        )

        # Assert — 据え置き。失敗の計上は `record_feed_health` の仕事なので増えない
        db_session.refresh(row)
        assert row.consecutive_stale_fetches == 5
        assert row.status == DiscoveredFeedStatus.FOUND.value
        assert row.enabled is True

    def test_is_more_tolerant_than_the_empty_fetch_threshold(self) -> None:
        """既出記事を返し続けること自体は異常ではないため、#108 の閾値より粘る。

        配信そのものが無いフィードは #108 側で先に無効化される。新着ゼロの判定が
        実際に効くのは「配信はあるが新着が無い」場合だけになる。
        """
        assert MAX_CONSECUTIVE_STALE_FETCHES > MAX_CONSECUTIVE_EMPTY_FETCHES


class TestRecordFeedNoveltySkipsDisabledRows:
    """受入基準: 既に無効化済みの行は検出対象に含めない。"""

    def test_does_not_touch_an_already_disabled_row_sharing_the_feed_url(
        self, db_session: Session
    ) -> None:
        """無効化済みの行は巡回対象ではない（`load_enabled_discovered_feeds` は
        `status=FOUND` かつ `enabled` のみを返す）。同じ `feed_url` を持つ別ドメインが
        まだ生きていると、その巡回結果が相乗りで届く。取得していない行を数えると
        カウンタが際限なく増え、無効化のログも巡回のたびに出続ける（cdf60fc で
        `record_feed_health` へ入れたガードと同じものを、この経路にも置く）。
        """
        # Arrange — 無効化済みの行と、同じ feed_url を指す生きた行
        feed_url = "https://mixed-stale.example.com/feed.xml"
        disabled = make_discovered_feed(
            db_session,
            domain="mixed-stale.example.com",
            feed_url=feed_url,
            status=DiscoveredFeedStatus.DISABLED,
            enabled=False,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES,
        )
        alive = make_discovered_feed(
            db_session, domain="blog.mixed-stale.example.com", feed_url=feed_url
        )

        # Act
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert — 無効化済みの行は据え置き、生きた行だけが数えられる
        db_session.refresh(disabled)
        assert disabled.consecutive_stale_fetches == MAX_CONSECUTIVE_STALE_FETCHES
        db_session.refresh(alive)
        assert alive.consecutive_stale_fetches == 1

    def test_does_not_reset_the_counter_of_an_already_disabled_row(
        self, db_session: Session
    ) -> None:
        """新着ありの結果が相乗りしても、無効化済みの行は復活させない。

        復活は再発見（`_apply_discovery_result`）が担う。ここで黙って
        カウンタだけ 0 へ戻すと、`status=DISABLED` のまま数字だけが健全に見える。
        """
        # Arrange
        feed_url = "https://revived-stale.example.com/feed.xml"
        disabled = make_discovered_feed(
            db_session,
            domain="revived-stale.example.com",
            feed_url=feed_url,
            status=DiscoveredFeedStatus.DISABLED,
            enabled=False,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES,
        )

        # Act
        record_feed_novelty(db_session, {feed_url: fresh()})

        # Assert
        db_session.refresh(disabled)
        assert disabled.consecutive_stale_fetches == MAX_CONSECUTIVE_STALE_FETCHES
        assert disabled.status == DiscoveredFeedStatus.DISABLED.value
        assert disabled.enabled is False


class TestRecordFeedNoveltyLogging:
    """受入基準: 「記事を配信しない」(#108) と「新着が出ない」(#109) を区別できる。"""

    def test_logs_a_distinct_event_from_the_empty_fetch_disablement(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """イベント名で見分ける。

        `caplog` ではなく `discovery_module.logger.info` を直接差し替える。
        `db_session` 経由で alembic の `env.py` が呼ぶ `logging.config.fileConfig`
        （既定で `disable_existing_loggers=True`）により、このモジュールの logger が
        セッション内で disabled になりうるため（`test_collectors_discovery.py` の
        同種テストと同じ理由）。
        """
        # Arrange
        info_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            discovery_module.logger, "info", lambda *args, **_kwargs: info_calls.append(args)
        )
        feed_url = "https://noisy-stale.example.com/feed.xml"
        make_discovered_feed(
            db_session,
            domain="noisy-stale.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 1,
        )

        # Act
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert — 新着ゼロ専用のイベント名が出る。#105 / #108 のイベント名は出ない
        # （"feed_disabled_empty" と "feed_disabled_stale" は "feed_disabled" に
        # 前方一致するため、素朴な部分文字列一致では区別できない）。
        messages = [call[0] for call in info_calls]
        assert any("collectors.discovery.feed_disabled_stale " in message for message in messages)
        assert not any(
            "collectors.discovery.feed_disabled_empty " in message for message in messages
        )
        assert not any("collectors.discovery.feed_disabled " in message for message in messages)

    def test_logs_the_counter_value_when_disabling(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """閾値の妥当性を後から検証できるよう、無効化時のカウンタ値をログへ出す。

        常駐監視も CI も持たないため、事後にログだけで追えるようにする。
        """
        # Arrange
        info_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            discovery_module.logger, "info", lambda *args, **_kwargs: info_calls.append(args)
        )
        feed_url = "https://counted-stale.example.com/feed.xml"
        make_discovered_feed(
            db_session,
            domain="counted-stale.example.com",
            feed_url=feed_url,
            consecutive_stale_fetches=MAX_CONSECUTIVE_STALE_FETCHES - 1,
        )

        # Act
        record_feed_novelty(db_session, {feed_url: stale()})

        # Assert — フォーマット引数にドメイン・フィード URL・カウンタ値が含まれる
        disablements = [
            call for call in info_calls if "collectors.discovery.feed_disabled_stale " in call[0]
        ]
        assert len(disablements) == 1
        assert "counted-stale.example.com" in disablements[0]
        assert feed_url in disablements[0]
        assert MAX_CONSECUTIVE_STALE_FETCHES in disablements[0]
