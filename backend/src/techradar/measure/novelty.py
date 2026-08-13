"""novelty 分布の集計（Issue #87、Issue #88）。

`compute_novelty` が topics の文字列一致で判定していた頃、既知トピックと 1 語も
重ならない候補が軒並み novelty = 1.0（上端）へ張り付き、diversity 枠が構造的に
選ばれなくなっていた。式は embedding のコサイン類似ベースへ差し替えたが
（`docs/adr/0007-embedding-based-novelty.md`）、`compute_interest_similarity`
（top_k 加重平均）と同じ embedding 空間を見るため両者の相関は残る。関心プロファイル
の偏り方によっては同じ縮退が別の形で再発しうるため、分布に加えて縮退の兆候そのもの
（相関・枠の偏り）も継続して観測できるようにこの集計を持つ。novelty の計算方法
そのものには関与しない。

出すのは 4 つ。

1. `rank_candidates` が返す `ScoredCandidate.breakdown.novelty` の分布
   （`summarize_novelty_distribution`）。上端への張り付き件数・割合と、現行の
   `exploration_min_novelty` を超える件数を必ず含める
2. その閾値を 0.0〜1.0 の 0.1 刻みで動かしたときに exploration 枠と diversity 枠が
   どう分岐するかの表（`summarize_threshold_table`）。閾値を実測で確定させる材料になる
3. novelty と interest_similarity の Spearman 順位相関（`summarize_novelty_interest_correlation`）。
   両者が同じ embedding 空間の表裏になっていないか、原因側から見る指標
4. strong_interest / primary_source を外れた候補が exploration と diversity のどちらへ
   偏っているか（`summarize_slot_divergence`）。ADR 0007 で実際に起きた症状
   （上位2枠に取られなかった候補が全て同じ枠へ流れる）を、症状側からそのまま数える

3 と 4 は原因と症状の対で意味を持つ。相関が -1 に近くても枠が両方へ分岐していれば
実害は無く、逆に相関が弱くても枠が偏っていれば別の要因で縮退している。どちらか片方
だけでは縮退の有無を読み違える。

枠の判定は `recommendation.composition._slot_for` をそのまま呼ぶ。優先順位
（strong_interest → primary_source → exploration → diversity）をここで再実装すると、
測っている対象が本番の挙動から外れるため。本番の実装を読む方針そのものは
`feed_slots.py` と同じだが、あちらが公開の `CompositionStats` だけを読むのに対し、
ここは private な `_slot_for` を跨いで呼んでいる点が違う。`composition.py` 側で
名前やシグネチャが変わっても型チェックでは気付けないため、そのときはこのモジュールの
テストが落ちることで検出する。

`_slot_for` は候補を 1 件ずつ分類するだけで、`compose_feed_with_stats` が行う定員の
適用・他枠からの補充・ドメイン重複の排除（`_select_primary`）は通らない。閾値表が
示すのは「その閾値なら各枠にいくつの候補が入りうるか」であって、最終的なフィードの
中身ではない。実際の充足率は `feed_slots.py` の集計で見る。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from techradar.recommendation.composition import FeedSlot, _slot_for
from techradar.recommendation.ranking import ScoredCandidate, ScoringSettings

# novelty の値域の上端（`NoveltyConfig` / `WeightsConfig` と同じ `le=1.0`）。
# これに張り付いている件数が Issue #87 の核心。
_SATURATION_VALUE = 1.0

# 閾値走査の刻み。yaml/コード中のリテラルと同じ浮動小数表現に揃えるため、割り算では
# なく直接書く（`i / 10` は多くの値で一致するが、比較境界のずれを避けたい）。
_THRESHOLD_CANDIDATES: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)


@dataclass(frozen=True)
class NoveltyDistribution:
    """novelty 値の分布。候補が 0 件なら分布系は `None` になる。"""

    candidate_count: int
    min_novelty: float | None
    p25: float | None
    p50: float | None
    p75: float | None
    p95: float | None
    max_novelty: float | None
    # 上端 (1.0) への張り付き。
    saturated_count: int
    saturated_ratio: float
    # 現行の exploration_min_novelty 以上の候補数（`_slot_for` と同じ `>=` で判定）。
    above_threshold_count: int
    exploration_min_novelty: float


@dataclass(frozen=True)
class ThresholdSlotCounts:
    """ある novelty 閾値を仮に使ったときの exploration / diversity 枠の件数。"""

    threshold: float
    exploration_count: int
    diversity_count: int


@dataclass(frozen=True)
class SlotDivergenceStats:
    """strong_interest / primary_source を外れた候補が exploration / diversity の
    どちらへ偏っているかの割合。

    `excluded_count` は exploration + diversity に入った候補数（＝上位2枠に取られ
    なかった候補数）、`diversity_count` はそのうち diversity に入った件数。
    `diversity_ratio` が 0% または 100% へ張り付いていたら、片方の枠へ候補が集中
    している＝縮退の兆候（ADR 0007 で実際に起きた症状）。`excluded_count` が 0
    （候補が無い、または全候補が上位2枠に収まる）なら比較対象が無いため `None`。
    """

    excluded_count: int
    diversity_count: int
    diversity_ratio: float | None


@dataclass(frozen=True)
class NoveltyStats:
    """novelty 分布・閾値走査の表・縮退の兆候（相関と枠の偏り）をまとめた集計。"""

    distribution: NoveltyDistribution
    threshold_table: tuple[ThresholdSlotCounts, ...]
    novelty_interest_correlation: float | None
    slot_divergence: SlotDivergenceStats


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """線形補間で分位点を求める。

    `statistics` モジュールには任意の分位点を 1 つだけ取れる関数が無いため自前で
    持つ。件数が 1 件のときは補間する余地が無いためその値をそのまま返す。
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    weight = position - lower_index
    return (
        sorted_values[lower_index]
        + (sorted_values[upper_index] - sorted_values[lower_index]) * weight
    )


def summarize_novelty_distribution(
    values: Sequence[float], *, exploration_min_novelty: float
) -> NoveltyDistribution:
    """novelty 値の列から分布をまとめる。

    最小 / 四分位 / p95 / 最大に加え、上端への張り付き件数・割合を必ず出す
    （Issue #87 の核心）。現行の `exploration_min_novelty` 以上の件数もあわせて出し、
    閾値を動かす前の基準値として使えるようにする。
    """
    if not values:
        return NoveltyDistribution(
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
            exploration_min_novelty=exploration_min_novelty,
        )

    ordered = sorted(values)
    saturated_count = sum(1 for value in ordered if value >= _SATURATION_VALUE)
    above_threshold_count = sum(1 for value in ordered if value >= exploration_min_novelty)

    return NoveltyDistribution(
        candidate_count=len(ordered),
        min_novelty=_percentile(ordered, 0.0),
        p25=_percentile(ordered, 0.25),
        p50=_percentile(ordered, 0.5),
        p75=_percentile(ordered, 0.75),
        p95=_percentile(ordered, 0.95),
        max_novelty=_percentile(ordered, 1.0),
        saturated_count=saturated_count,
        saturated_ratio=saturated_count / len(ordered),
        above_threshold_count=above_threshold_count,
        exploration_min_novelty=exploration_min_novelty,
    )


def summarize_threshold_table(
    scored: Sequence[ScoredCandidate], settings: ScoringSettings
) -> tuple[ThresholdSlotCounts, ...]:
    """`exploration_min_novelty` を 0.0〜1.0 の 0.1 刻みで動かした表を作る。

    各刻みについて、`_slot_for` を（閾値だけを差し替えた `ScoringSettings` で）
    そのまま呼び、exploration 枠と diversity 枠に入る件数を数える。
    `_slot_for` は strong_interest → primary_source → exploration → diversity の
    優先順位で判定するため、strong_interest / primary_source に先に該当する候補は、
    閾値を動かしても exploration / diversity には移らない（表の合計が候補総数と
    一致しないのはこのため）。
    """
    rows = []
    for threshold in _THRESHOLD_CANDIDATES:
        trial_settings = replace(
            settings,
            feed_composition=replace(settings.feed_composition, exploration_min_novelty=threshold),
        )
        exploration_count = 0
        diversity_count = 0
        for item in scored:
            slot = _slot_for(item, trial_settings)
            if slot is FeedSlot.EXPLORATION:
                exploration_count += 1
            elif slot is FeedSlot.DIVERSITY:
                diversity_count += 1
        rows.append(
            ThresholdSlotCounts(
                threshold=threshold,
                exploration_count=exploration_count,
                diversity_count=diversity_count,
            )
        )
    return tuple(rows)


def _average_ranks(values: Sequence[float]) -> list[float]:
    """値の列に 1 始まりの順位を付ける。同順位 (tie) は平均順位にする。

    Spearman の順位相関は同順位を無視すると値がずれるため、`statistics` に無いこの
    処理を自前で持つ。例えば `(10, 20, 20, 30)` の順位は `(1, 2.5, 2.5, 4)` になる
    （2 位と 3 位を分け合う）。
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        # i 番目から j 番目まで（0 始まり）が同順位。1 始まりの平均順位に直す。
        average_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average_rank
        i = j + 1
    return ranks


def _spearman_correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman の順位相関係数を求める。

    順位に変換したうえで Pearson の相関係数と同じ式（共分散 / 標準偏差の積）を適用する。
    これは同順位を平均順位で扱う一般化であり、同順位が無ければ教科書の
    `1 - 6Σd² / (n(n²-1))` と一致する。分散ゼロ（全値が同じ、または件数が 2 未満）
    のときは相関を定義できないため `None` を返す（例外にしない）。
    """
    count = len(xs)
    if count < 2:
        return None

    rank_x = _average_ranks(xs)
    rank_y = _average_ranks(ys)
    mean_x = sum(rank_x) / count
    mean_y = sum(rank_y) / count

    covariance = sum((rx - mean_x) * (ry - mean_y) for rx, ry in zip(rank_x, rank_y, strict=True))
    variance_x = sum((rx - mean_x) ** 2 for rx in rank_x)
    variance_y = sum((ry - mean_y) ** 2 for ry in rank_y)
    if variance_x == 0.0 or variance_y == 0.0:
        return None

    return covariance / (variance_x * variance_y) ** 0.5


def summarize_novelty_interest_correlation(scored: Sequence[ScoredCandidate]) -> float | None:
    """novelty と interest_similarity の Spearman 順位相関を求める。

    両者は同じ embedding 空間を見ている（novelty は関心記事群への最大類似度の裏返し、
    interest_similarity は top_k 加重平均）ため相関が残る（ADR 0007「影響」節）。
    -1.0 に近いほど、枠判定の 1 段目（strong_interest、interest_similarity で判定）と
    3 段目（exploration、novelty で判定）が実質同じ軸の表裏になっていることを示す。
    Pearson ではなく Spearman を使うのは、知りたいのが線形性ではなく「並べ直したとき
    同じ順になるか」だから。両方が単調な関係ではあっても線形とは限らない。
    """
    novelty_values = [item.breakdown.novelty for item in scored]
    interest_values = [item.breakdown.interest_similarity for item in scored]
    return _spearman_correlation(novelty_values, interest_values)


def summarize_slot_divergence(
    scored: Sequence[ScoredCandidate], settings: ScoringSettings
) -> SlotDivergenceStats:
    """strong_interest / primary_source を外れた候補が exploration / diversity の
    どちらへ偏っているかを数える（ADR 0007 で実際に起きた症状をそのまま数える）。

    `summarize_threshold_table` と違い閾値は動かさず、現在の `settings` の
    `exploration_min_novelty` 1 点だけで判定する。閾値走査は候補側（どの閾値なら
    分岐するか）を見るためのもの、こちらは今の設定での実際の偏りを見るためのもの。
    """
    excluded_count = 0
    diversity_count = 0
    for item in scored:
        slot = _slot_for(item, settings)
        if slot is FeedSlot.EXPLORATION:
            excluded_count += 1
        elif slot is FeedSlot.DIVERSITY:
            excluded_count += 1
            diversity_count += 1

    diversity_ratio = diversity_count / excluded_count if excluded_count > 0 else None
    return SlotDivergenceStats(
        excluded_count=excluded_count,
        diversity_count=diversity_count,
        diversity_ratio=diversity_ratio,
    )


def summarize_novelty(scored: Sequence[ScoredCandidate], settings: ScoringSettings) -> NoveltyStats:
    """採点済み候補から novelty の分布・閾値走査・縮退の兆候をまとめる。"""
    values = tuple(item.breakdown.novelty for item in scored)
    distribution = summarize_novelty_distribution(
        values, exploration_min_novelty=settings.feed_composition.exploration_min_novelty
    )
    threshold_table = summarize_threshold_table(scored, settings)
    correlation = summarize_novelty_interest_correlation(scored)
    slot_divergence = summarize_slot_divergence(scored, settings)
    return NoveltyStats(
        distribution=distribution,
        threshold_table=threshold_table,
        novelty_interest_correlation=correlation,
        slot_divergence=slot_divergence,
    )
