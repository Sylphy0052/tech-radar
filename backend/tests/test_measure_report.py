"""計測結果のまとめと出力（`techradar.measure.report`）のテスト（Issue #74）。

計測結果は Issue へコメントで残す運用（Issue #73 の検証方法）のため、人が読む
表形式と、後から機械で扱える JSON の両方を出す。データが 0 件でも「対象データが無い」
と分かる出力にし、異常終了させない。
"""

from __future__ import annotations

import json

from techradar.measure.body_length import BodyLengthStats
from techradar.measure.clusters import ClusterStats, ClusterSummary
from techradar.measure.feed_slots import FeedCompositionStats, FeedSlotStats
from techradar.measure.report import Measurements, render_json, render_text

_EMPTY = Measurements(
    body_length=BodyLengthStats(
        article_count=0,
        min_length=None,
        median_length=None,
        max_length=None,
        truncated_count=0,
        truncated_ratio=0.0,
        limit=12000,
    ),
    clusters=ClusterStats(source_count=0, cluster_count=0, clusters=()),
    feed=FeedCompositionStats(candidate_count=0, page_size=20, slots=()),
)

_FILLED = Measurements(
    body_length=BodyLengthStats(
        article_count=3,
        min_length=100,
        median_length=5000,
        max_length=30000,
        truncated_count=1,
        truncated_ratio=1 / 3,
        limit=12000,
    ),
    clusters=ClusterStats(
        source_count=5,
        cluster_count=2,
        clusters=(
            ClusterSummary(label="Kubernetes", weight=0.6, topics=("k8s",), article_count=3),
            ClusterSummary(label="Rust", weight=0.4, topics=("rust",), article_count=2),
        ),
    ),
    feed=FeedCompositionStats(
        candidate_count=7,
        page_size=20,
        slots=(
            FeedSlotStats(
                slot="strong_interest", quota=11, selected=4, backfilled=1, fill_rate=4 / 11
            ),
        ),
    ),
)


class TestRenderText:
    def test_states_when_there_is_no_data(self) -> None:
        """データが無いことを明示する。空の表だけだと、壊れているのか空なのか分からない。"""
        output = render_text(_EMPTY)

        assert "対象データがありません" in output

    def test_includes_all_three_sections(self) -> None:
        output = render_text(_FILLED)

        assert "本文長" in output
        assert "関心クラスタ" in output
        assert "フィード枠" in output

    def test_shows_truncation_ratio_as_percentage(self) -> None:
        output = render_text(_FILLED)

        assert "33.3%" in output

    def test_lists_each_cluster_with_article_count(self) -> None:
        output = render_text(_FILLED)

        assert "Kubernetes" in output
        assert "Rust" in output

    def test_shows_the_configured_limit(self) -> None:
        """上限値そのものを出す。どの値に対する切り捨て率かが分かるようにする。"""
        output = render_text(_FILLED)

        assert "12000" in output


class TestRenderJson:
    def test_produces_parsable_json(self) -> None:
        parsed = json.loads(render_json(_FILLED))

        assert parsed["body_length"]["article_count"] == 3
        assert parsed["body_length"]["limit"] == 12000
        assert parsed["clusters"]["cluster_count"] == 2
        assert parsed["clusters"]["clusters"][0]["label"] == "Kubernetes"
        assert parsed["feed"]["slots"][0]["slot"] == "strong_interest"

    def test_keeps_none_for_missing_values(self) -> None:
        """0 件のときの長さは 0 ではなく null にする。0 文字の記事と区別する。"""
        parsed = json.loads(render_json(_EMPTY))

        assert parsed["body_length"]["median_length"] is None

    def test_is_not_ascii_escaped(self) -> None:
        """日本語のラベルをそのまま読めるようにする。"""
        measurements = Measurements(
            body_length=_EMPTY.body_length,
            clusters=ClusterStats(
                source_count=1,
                cluster_count=1,
                clusters=(
                    ClusterSummary(
                        label="日本語ラベル", weight=1.0, topics=("トピック",), article_count=1
                    ),
                ),
            ),
            feed=_EMPTY.feed,
        )

        assert "日本語ラベル" in render_json(measurements)
