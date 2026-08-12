"""記事本文長の集計（`techradar.measure.body_length`）のテスト（Issue #74）。

`MAX_ANALYSIS_BODY_CHARACTERS` の 12000 という値には根拠が残っていない（Issue #73）。
確定させるには、まず実際の記事本文がどれくらいの長さで、上限で切り捨てられる記事が
どれだけあるかを知る必要がある。ここではその集計の入出力を固定する。

集計は純粋関数（長さの列 → 統計）と DB 読み取りに分け、値の正しさは純粋関数側で確かめる。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from techradar.db.models import Article
from techradar.measure.body_length import (
    BodyLengthStats,
    load_body_lengths,
    summarize_body_lengths,
)


class TestSummarizeBodyLengths:
    def test_returns_empty_stats_for_no_articles(self) -> None:
        """対象が無くても失敗しない。データが揃う前に実行されるため。"""
        stats = summarize_body_lengths([], limit=12000)

        assert stats == BodyLengthStats(
            article_count=0,
            min_length=None,
            median_length=None,
            max_length=None,
            truncated_count=0,
            truncated_ratio=0.0,
            limit=12000,
        )

    def test_computes_min_median_max(self) -> None:
        """最小・中央値・最大を返す。"""
        stats = summarize_body_lengths([100, 300, 200], limit=12000)

        assert stats.article_count == 3
        assert stats.min_length == 100
        assert stats.median_length == 200
        assert stats.max_length == 300

    def test_median_of_even_count_averages_middle_two(self) -> None:
        """件数が偶数なら中央 2 件の平均を採る。"""
        stats = summarize_body_lengths([100, 200, 300, 400], limit=12000)

        assert stats.median_length == 250

    def test_rounds_a_fractional_median(self) -> None:
        """中央 2 件の平均が小数になるときは丸める。

        切り捨てると件数が偶数のときだけ系統的に下振れするため `round` を使う。
        `.5` の扱いは Python の既定（偶数側へ丸める）に従う。文字数の統計として
        1 文字未満の差は判断に影響しないため、独自の丸めは持ち込まない。
        """
        assert summarize_body_lengths([100, 103], limit=12000).median_length == 102
        assert summarize_body_lengths([100, 101], limit=12000).median_length == 100

    def test_counts_articles_over_the_limit(self) -> None:
        """上限を超える記事の件数と割合を出す。これが切り捨ての発生率になる。"""
        stats = summarize_body_lengths([100, 12000, 12001, 30000], limit=12000)

        # ちょうど上限の記事は切り捨てられない（`body[:limit]` は 12000 文字をそのまま残す）。
        assert stats.truncated_count == 2
        assert stats.truncated_ratio == pytest.approx(0.5)

    def test_ratio_is_zero_when_nothing_is_truncated(self) -> None:
        stats = summarize_body_lengths([10, 20], limit=12000)

        assert stats.truncated_count == 0
        assert stats.truncated_ratio == 0.0


class TestLoadBodyLengths:
    def test_returns_empty_for_empty_table(self, db_session: Session) -> None:
        assert load_body_lengths(db_session) == ()

    def test_reads_lengths_of_stored_bodies(self, db_session: Session) -> None:
        """本文そのものではなく長さだけを読む。本文を全件メモリへ載せないため。"""
        db_session.add(
            Article(
                canonical_url="https://example.com/a",
                original_url="https://example.com/a",
                source_domain="example.com",
                title="A",
                body="x" * 150,
            )
        )
        db_session.add(
            Article(
                canonical_url="https://example.com/b",
                original_url="https://example.com/b",
                source_domain="example.com",
                title="B",
                body="y" * 50,
            )
        )
        db_session.flush()

        assert sorted(load_body_lengths(db_session)) == [50, 150]

    def test_skips_articles_without_body(self, db_session: Session) -> None:
        """本文が無い記事は解析に回らないため、分布の対象から外す。"""
        db_session.add(
            Article(
                canonical_url="https://example.com/c",
                original_url="https://example.com/c",
                source_domain="example.com",
                title="C",
                body=None,
            )
        )
        db_session.flush()

        assert load_body_lengths(db_session) == ()
