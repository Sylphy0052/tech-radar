"""本文長ごとの LLM 応答時間の実測エントリポイント（`techradar.measure.run_llm_latency`）の
テスト（Issue #73）。

`MAX_ANALYSIS_BODY_CHARACTERS` の根拠を実測で確定するには、通常の技術記事を代表する
記事で測る必要がある。本番実測では最長記事が全リリースノートを連結した 546409 文字の
外れ値だったため、代表記事は「測る最大長を賄える記事のうち短い順」に選ぶ方式へ変えた。
ここでは引数パース・記事選定・記事ごと/全体のレンダリングをそれぞれ固定する。
LLM は実際には呼ばず `FakeLLMProvider` を使う。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from techradar.db.models import Article
from techradar.llm.errors import LLMError
from techradar.llm.fake import FakeLLMProvider
from techradar.measure.llm_latency import LatencySample
from techradar.measure.run_llm_latency import (
    ArticleLatencyResult,
    MeasurementArticle,
    _load_measurement_bodies,
    _measure_article,
    _overall_stats,
    _parse_args,
    _render_json,
    _render_text,
    _truncate_url,
)

_RESPONSE = (
    '{"translated_title": "題", "summary_ja": "要約", "domain": "AI", "category": "LLM", '
    '"topics": ["t"], "technologies": ["x"], "content_type": "news", '
    '"difficulty": "beginner", "technical_quality": 0.5}'
)


def _article_row(session: Session, *, slug: str, body: str | None) -> Article:
    article = Article(
        canonical_url=f"https://example.com/{slug}",
        original_url=f"https://example.com/{slug}",
        source_domain="example.com",
        title=slug,
        body=body,
    )
    session.add(article)
    session.flush()
    return article


class TestParseArgs:
    def test_defaults_articles_to_two(self) -> None:
        """既定は 2 件。1 記事だけだと外れ値を引いたときに気付けない。"""
        args = _parse_args([])

        assert args.articles == 2

    def test_accepts_an_explicit_article_count(self) -> None:
        args = _parse_args(["--articles", "5"])

        assert args.articles == 5

    def test_rejects_zero_articles(self) -> None:
        """0 件では測りようがないため、argparse のエラーとして落とす。"""
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--articles", "0"])

        assert exc_info.value.code != 0

    def test_rejects_negative_articles(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--articles", "-1"])

        assert exc_info.value.code != 0

    def test_rejects_non_integer_articles(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--articles", "two"])

        assert exc_info.value.code != 0


class TestTruncateUrl:
    def test_keeps_short_urls_unchanged(self) -> None:
        assert _truncate_url("https://example.com/a") == "https://example.com/a"

    def test_truncates_long_urls_with_an_ellipsis(self) -> None:
        """記事一覧の表示行を潰さないよう、長い URL は appendix を省略する。"""
        url = "https://example.com/" + "a" * 100

        truncated = _truncate_url(url, limit=20)

        assert len(truncated) == 20
        assert truncated.endswith("…")


class TestLoadMeasurementBodies:
    def test_returns_empty_when_no_article_meets_the_minimum(self, db_session: Session) -> None:
        _article_row(db_session, slug="short", body="x" * 100)

        articles = _load_measurement_bodies(db_session, min_length=200, count=2)

        assert articles == ()

    def test_selects_the_shortest_articles_above_the_minimum(self, db_session: Session) -> None:
        """外れ値（最長記事）ではなく、賄える中で最も短い記事から選ぶ。"""
        _article_row(db_session, slug="too-short", body="x" * 100)
        _article_row(db_session, slug="shortest", body="x" * 300)
        _article_row(db_session, slug="middle", body="x" * 500)
        _article_row(db_session, slug="outlier", body="x" * 900)

        articles = _load_measurement_bodies(db_session, min_length=200, count=2)

        assert [a.canonical_url for a in articles] == [
            "https://example.com/shortest",
            "https://example.com/middle",
        ]

    def test_skips_articles_without_a_body(self, db_session: Session) -> None:
        _article_row(db_session, slug="no-body", body=None)
        _article_row(db_session, slug="has-body", body="x" * 300)

        articles = _load_measurement_bodies(db_session, min_length=200, count=5)

        assert [a.canonical_url for a in articles] == ["https://example.com/has-body"]

    def test_limits_to_the_requested_count(self, db_session: Session) -> None:
        for index in range(3):
            _article_row(db_session, slug=f"a{index}", body="x" * (300 + index))

        articles = _load_measurement_bodies(db_session, min_length=200, count=1)

        assert len(articles) == 1


class TestMeasureArticle:
    def test_collects_a_sample_per_length_and_repeat(self) -> None:
        provider = FakeLLMProvider([_RESPONSE])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 10)

        result = _measure_article(provider, article, lengths=[4, 8], repeats=2, index=1, total=1)

        assert [sample.length for sample in result.samples] == [4, 4, 8, 8]
        assert all(sample.ok for sample in result.samples)

    def test_prints_progress_with_the_article_label(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """進捗は記事が分かる形で stderr へ出す。数十分かかる実行の途中経過のため。"""
        provider = FakeLLMProvider([_RESPONSE])
        article = MeasurementArticle(canonical_url="https://example.com/progress", body="x" * 10)

        _measure_article(provider, article, lengths=[4], repeats=1, index=2, total=3)

        captured = capsys.readouterr()
        assert "[2/3]" in captured.err
        assert "https://example.com/progress" in captured.err

    def test_records_failures_without_raising(self) -> None:
        provider = FakeLLMProvider([LLMError("失敗")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 10)

        result = _measure_article(provider, article, lengths=[4], repeats=1, index=1, total=1)

        assert result.samples[0].ok is False


class TestOverallStats:
    def test_combines_samples_across_articles_by_length(self) -> None:
        """記事横断の全体集計は、同じ長さのサンプルをまとめる。"""
        results = [
            ArticleLatencyResult(
                article=MeasurementArticle(canonical_url="https://example.com/a", body="x"),
                samples=(LatencySample(length=1000, seconds=1.0, ok=True),),
            ),
            ArticleLatencyResult(
                article=MeasurementArticle(canonical_url="https://example.com/b", body="x"),
                samples=(LatencySample(length=1000, seconds=3.0, ok=True),),
            ),
        ]

        stats = _overall_stats(results)

        assert len(stats) == 1
        assert stats[0].samples == 2
        assert stats[0].median_seconds == pytest.approx(2.0)


class TestRenderText:
    def test_includes_each_article_and_the_overall_summary(self) -> None:
        results = [
            ArticleLatencyResult(
                article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 5),
                samples=(LatencySample(length=4, seconds=1.0, ok=True),),
            ),
            ArticleLatencyResult(
                article=MeasurementArticle(canonical_url="https://example.com/b", body="x" * 9),
                samples=(LatencySample(length=4, seconds=3.0, ok=True),),
            ),
        ]

        rendered = _render_text(results, repeats=1)

        assert "https://example.com/a" in rendered
        assert "https://example.com/b" in rendered
        assert "全体（記事 2 件横断）:" in rendered
        # 全体集計は 2 記事分のサンプルを合わせた中央値になる。
        assert "中央値    2.0 秒" in rendered

    def test_marks_a_length_with_only_failures(self) -> None:
        results = [
            ArticleLatencyResult(
                article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 5),
                samples=(LatencySample(length=4, seconds=0.5, ok=False),),
            )
        ]

        rendered = _render_text(results, repeats=1)

        assert "全て失敗（1 回）" in rendered


class TestRenderJson:
    def test_includes_articles_and_overall_sections(self) -> None:
        results = [
            ArticleLatencyResult(
                article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 5),
                samples=(LatencySample(length=4, seconds=1.0, ok=True),),
            )
        ]

        parsed = json.loads(_render_json(results))

        assert parsed["articles"][0]["canonical_url"] == "https://example.com/a"
        assert parsed["articles"][0]["body_length"] == 5
        assert parsed["articles"][0]["stats"][0]["length"] == 4
        assert parsed["overall"][0]["length"] == 4
