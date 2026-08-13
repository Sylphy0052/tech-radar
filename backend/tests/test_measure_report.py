"""計測結果のまとめと出力（`techradar.measure.report`）のテスト（Issue #74、Issue #87）。

計測結果は Issue へコメントで残す運用（Issue #73 の検証方法）のため、人が読む
表形式と、後から機械で扱える JSON の両方を出す。データが 0 件でも「対象データが無い」
と分かる出力にし、異常終了させない。
"""

from __future__ import annotations

import json

from techradar.measure.body_length import BodyLengthStats
from techradar.measure.clusters import ClusterStats, ClusterSummary
from techradar.measure.feed_slots import FeedCompositionStats, FeedSlotStats
from techradar.measure.novelty import NoveltyDistribution, NoveltyStats, ThresholdSlotCounts
from techradar.measure.report import Measurements, render_json, render_text

_EMPTY_NOVELTY = NoveltyStats(
    distribution=NoveltyDistribution(
        candidate_count=0,
        min_novelty=None,
        p25=None,
        p50=None,
        p75=None,
        p95=None,
        max_novelty=None,
        saturated_count=0,
        saturated_ratio=0.0,
        above_threshold_count=0,
        exploration_min_novelty=0.6,
    ),
    threshold_table=(),
)

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
    novelty=_EMPTY_NOVELTY,
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
    novelty=NoveltyStats(
        distribution=NoveltyDistribution(
            candidate_count=4,
            min_novelty=0.1,
            p25=0.2,
            p50=0.5,
            p75=1.0,
            p95=1.0,
            max_novelty=1.0,
            saturated_count=2,
            saturated_ratio=0.5,
            above_threshold_count=2,
            exploration_min_novelty=0.6,
        ),
        threshold_table=(
            ThresholdSlotCounts(threshold=0.6, exploration_count=2, diversity_count=2),
        ),
    ),
)


class TestRenderText:
    def test_states_when_there_is_no_data(self) -> None:
        """データが無いことを明示する。空の表だけだと、壊れているのか空なのか分からない。"""
        output = render_text(_EMPTY)

        assert "対象データがありません" in output

    def test_includes_all_four_sections(self) -> None:
        output = render_text(_FILLED)

        assert "本文長" in output
        assert "関心クラスタ" in output
        assert "フィード枠" in output
        assert "novelty分布" in output

    def test_shows_novelty_saturation_and_threshold_table(self) -> None:
        """1.0 への張り付き件数・割合と、閾値ごとの分岐表を出す（Issue #87 の核心）。"""
        output = render_text(_FILLED)

        assert "1.0への張り付き: 2 件（50.0%）" in output
        assert "0.6: exploration 2 / diversity 2" in output

    def test_shows_truncation_ratio_as_percentage(self) -> None:
        output = render_text(_FILLED)

        assert "33.3%" in output

    def test_lists_each_cluster_with_article_count(self) -> None:
        output = render_text(_FILLED)

        assert "Kubernetes" in output
        assert "Rust" in output

    def test_states_no_data_when_candidates_are_zero_but_quotas_exist(self) -> None:
        """候補 0 件でも枠の定員は返る。実運用で起きるのはこちらの形。

        `compose_feed_with_stats` は候補が無くても 4 枠ぶんの `SlotStats` を返すため、
        枠の一覧が空になることは実行時にはほぼ無い。定員だけが並ぶと「データがあるのに
        選ばれていない」のか「そもそもデータが無い」のか読み取れない。
        """
        measurements = Measurements(
            body_length=_EMPTY.body_length,
            clusters=_EMPTY.clusters,
            feed=FeedCompositionStats(
                candidate_count=0,
                page_size=100,
                slots=(
                    FeedSlotStats(
                        slot="strong_interest", quota=55, selected=0, backfilled=0, fill_rate=0.0
                    ),
                ),
            ),
            novelty=_EMPTY_NOVELTY,
        )

        output = render_text(measurements)

        assert "対象データがありません" in output
        assert "strong_interest" in output

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
        assert parsed["novelty"]["distribution"]["saturated_count"] == 2
        assert parsed["novelty"]["threshold_table"][0]["threshold"] == 0.6

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
            novelty=_EMPTY_NOVELTY,
        )

        assert "日本語ラベル" in render_json(measurements)
