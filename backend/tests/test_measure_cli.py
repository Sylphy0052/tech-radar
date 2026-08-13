"""計測 CLI（`techradar.measure.__main__`）のテスト（Issue #74）。

CLI は DB へ接続する。ここでは引数の解釈と出力形式の切り替えだけを確かめ、DB 接続は
差し替える。接続まで含めた確認は `test_measure_collect.py` が担う。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from techradar.measure import __main__ as cli
from techradar.measure.body_length import BodyLengthStats
from techradar.measure.clusters import ClusterStats, ClusterSummary
from techradar.measure.feed_slots import FeedCompositionStats
from techradar.measure.novelty import NoveltyDistribution, NoveltyStats
from techradar.measure.report import Measurements

_MEASUREMENTS = Measurements(
    body_length=BodyLengthStats(
        article_count=2,
        min_length=10,
        median_length=20,
        max_length=30,
        truncated_count=0,
        truncated_ratio=0.0,
        limit=12000,
    ),
    clusters=ClusterStats(
        source_count=1,
        cluster_count=1,
        clusters=(ClusterSummary(label="Go", weight=1.0, topics=("go",), article_count=1),),
    ),
    feed=FeedCompositionStats(candidate_count=0, page_size=20, slots=()),
    novelty=NoveltyStats(
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
    ),
)


@pytest.fixture
def stubbed_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 接続と集計を差し替える。CLI の責務（引数と出力）だけを見る。"""

    @contextmanager
    def fake_session() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(cli, "read_only_session", fake_session)
    monkeypatch.setattr(cli, "collect_measurements", lambda *args, **kwargs: _MEASUREMENTS)


@pytest.mark.usefixtures("stubbed_collect")
class TestMain:
    def test_prints_text_by_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = cli.main([])

        assert exit_code == 0
        assert "本文長" in capsys.readouterr().out

    def test_prints_json_with_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = cli.main(["--json"])

        assert exit_code == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["clusters"]["clusters"][0]["label"] == "Go"

    def test_rejects_unknown_option(self) -> None:
        """未知の引数は黙って無視せず、使い方を示して終わる。"""
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--unknown"])

        assert exc_info.value.code != 0
