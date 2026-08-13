"""計測結果のまとめと出力（Issue #74、Issue #87）。

計測結果は Issue へコメントで残す運用（Issue #73 の検証方法）のため、人が読む表形式と、
後から機械で扱える JSON の両方を出す。出力先はファイルではなく標準出力にし、必要なら
リダイレクトさせる。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from techradar.measure.body_length import BodyLengthStats
from techradar.measure.clusters import ClusterStats
from techradar.measure.feed_slots import FeedCompositionStats
from techradar.measure.novelty import NoveltyStats

_NO_DATA = "対象データがありません"


@dataclass(frozen=True)
class Measurements:
    """4 項目の計測結果。"""

    body_length: BodyLengthStats
    clusters: ClusterStats
    feed: FeedCompositionStats
    novelty: NoveltyStats


def _format_ratio(value: float) -> str:
    return f"{value * 100:.1f}%"


def _render_body_length(stats: BodyLengthStats) -> list[str]:
    lines = [f"## 本文長（上限 {stats.limit} 文字）", ""]
    if stats.article_count == 0:
        lines.append(_NO_DATA)
        return lines

    lines.extend(
        [
            f"記事数: {stats.article_count}",
            f"最小: {stats.min_length} / 中央値: {stats.median_length} / 最大: {stats.max_length}",
            f"上限超過: {stats.truncated_count} 件（{_format_ratio(stats.truncated_ratio)}）",
        ]
    )
    return lines


def _render_clusters(stats: ClusterStats) -> list[str]:
    lines = ["## 関心クラスタ", ""]
    if stats.source_count == 0:
        lines.append(_NO_DATA)
        return lines

    lines.append(f"対象記事数: {stats.source_count} / クラスタ数: {stats.cluster_count}")
    lines.append("")
    for cluster in stats.clusters:
        topics = " / ".join(cluster.topics) if cluster.topics else "-"
        lines.append(
            f"- {cluster.label}: {cluster.article_count} 件"
            f"（重み {cluster.weight:.3f}、topics: {topics}）"
        )
    return lines


def _render_feed(stats: FeedCompositionStats) -> list[str]:
    lines = [f"## フィード枠（1 回の実行で選ぶ件数 {stats.page_size}）", ""]
    # 候補が 0 件でも `compose_feed_with_stats` は 4 枠ぶんの定員を返す。他の 2 項目と
    # 表示を揃えるため、候補が無いことを先に明示してから定員を見せる。
    if stats.candidate_count == 0:
        lines.append(_NO_DATA)
        if not stats.slots:
            return lines
        lines.append("")

    lines.append(f"採点済み候補数: {stats.candidate_count}")
    lines.append("")
    for slot in stats.slots:
        lines.append(
            f"- {slot.slot}: 定員 {slot.quota} / 選択 {slot.selected}"
            f"（充足 {_format_ratio(slot.fill_rate)}、うち補充 {slot.backfilled}）"
        )
    return lines


def _render_novelty(stats: NoveltyStats) -> list[str]:
    lines = ["## novelty分布", ""]
    distribution = stats.distribution
    if distribution.candidate_count == 0:
        lines.append(_NO_DATA)
        return lines

    lines.extend(
        [
            f"候補数: {distribution.candidate_count}",
            f"最小: {distribution.min_novelty:.3f} / p25: {distribution.p25:.3f} / "
            f"p50: {distribution.p50:.3f} / p75: {distribution.p75:.3f} / "
            f"p95: {distribution.p95:.3f} / 最大: {distribution.max_novelty:.3f}",
            f"1.0への張り付き: {distribution.saturated_count} 件"
            f"（{_format_ratio(distribution.saturated_ratio)}）",
            f"現行の exploration_min_novelty（{distribution.exploration_min_novelty:.3f}）以上: "
            f"{distribution.above_threshold_count} 件",
            "",
            "### 閾値ごとの exploration / diversity 件数",
        ]
    )
    for row in stats.threshold_table:
        lines.append(
            f"- {row.threshold:.1f}: exploration {row.exploration_count} / "
            f"diversity {row.diversity_count}"
        )
    return lines


def render_text(measurements: Measurements) -> str:
    """人が読む形式でまとめる。"""
    sections = [
        _render_body_length(measurements.body_length),
        _render_clusters(measurements.clusters),
        _render_feed(measurements.feed),
        _render_novelty(measurements.novelty),
    ]
    return "\n\n".join("\n".join(section) for section in sections)


def to_dict(measurements: Measurements) -> dict[str, Any]:
    """JSON へ落とせる形に変換する。"""
    return asdict(measurements)


def render_json(measurements: Measurements) -> str:
    """機械可読な JSON にする。日本語のラベルはそのまま読める形で出す。"""
    return json.dumps(to_dict(measurements), ensure_ascii=False, indent=2)
