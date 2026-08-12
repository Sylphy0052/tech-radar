"""切り捨てが解析結果へ与える影響の実測エントリポイント
（`techradar.measure.run_truncation_impact`）のテスト（Issue #73）。

応答時間側は `run_llm_latency.py` で実測済みで、本文長にほぼ依存しないと分かっている。
残る論点は品質側で、同じ記事を「切り捨て版」「全文版」の 2 回解析して比較する必要が
ある。ここでは引数パース・記事選定・失敗時の扱い・レンダリングをそれぞれ固定する。
LLM は実際には呼ばず `FakeLLMProvider` を使う。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy.orm import Session

from techradar.analysis.schema import ArticleAnalysis
from techradar.analysis.service import MAX_ANALYSIS_BODY_CHARACTERS
from techradar.db.enums import ContentType, Difficulty
from techradar.db.models import Article
from techradar.llm.errors import LLMError, LLMManagedPolicyDetectedError, LLMToolUseDetectedError
from techradar.llm.fake import FakeLLMProvider
from techradar.measure import run_truncation_impact as cli
from techradar.measure.run_truncation_impact import (
    ArticleComparisonResult,
    ComparisonFailure,
    MeasurementArticle,
    _compare_article,
    _load_measurement_bodies,
    _parse_args,
    _render_json,
    _render_text,
)
from techradar.measure.truncation_impact import compare_analyses

_RESPONSE_A = (
    '{"translated_title": "題A", "summary_ja": "要約A", "domain": "AI", "category": "LLM", '
    '"topics": ["t"], "technologies": ["x"], "content_type": "news", '
    '"difficulty": "beginner", "technical_quality": 0.5}'
)
_RESPONSE_B = (
    '{"translated_title": "題B", "summary_ja": "要約B", "domain": "AI", "category": "Data", '
    '"topics": ["t", "u"], "technologies": ["x"], "content_type": "news", '
    '"difficulty": "intermediate", "technical_quality": 0.7}'
)


def _fake_analysis(*, domain: str = "AI") -> ArticleAnalysis:
    """比較関数のテスト用に最小限の `ArticleAnalysis` を作る。"""
    return ArticleAnalysis(
        translated_title="題",
        summary_ja="要約",
        domain=domain,
        category="LLM",
        topics=["t"],
        technologies=["x"],
        content_type=ContentType.NEWS,
        difficulty=Difficulty.BEGINNER,
        technical_quality=0.5,
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
    def test_defaults_articles_to_three(self) -> None:
        """既定は 3 件。"""
        args = _parse_args([])

        assert args.articles == 3

    def test_defaults_limit_to_the_analysis_constant(self) -> None:
        """既定の上限は解析側と同じ `MAX_ANALYSIS_BODY_CHARACTERS`。"""
        args = _parse_args([])

        assert args.limit == MAX_ANALYSIS_BODY_CHARACTERS

    def test_accepts_explicit_articles_and_limit(self) -> None:
        args = _parse_args(["--articles", "5", "--limit", "8000"])

        assert args.articles == 5
        assert args.limit == 8000

    def test_rejects_zero_articles(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--articles", "0"])

        assert exc_info.value.code != 0

    def test_rejects_non_integer_articles(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--articles", "three"])

        assert exc_info.value.code != 0

    def test_rejects_zero_limit(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--limit", "0"])

        assert exc_info.value.code != 0

    def test_rejects_non_integer_limit(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _parse_args(["--limit", "many"])

        assert exc_info.value.code != 0

    def test_json_flag_defaults_to_false(self) -> None:
        args = _parse_args([])

        assert args.as_json is False

    def test_json_flag_can_be_set(self) -> None:
        args = _parse_args(["--json"])

        assert args.as_json is True


class TestLoadMeasurementBodies:
    def test_returns_empty_when_no_article_exceeds_the_limit(self, db_session: Session) -> None:
        """上限を超えない記事は解析時に切り捨てられないため対象外にする。"""
        _article_row(db_session, slug="short", body="x" * 100)

        articles = _load_measurement_bodies(db_session, limit=200, count=2)

        assert articles == ()

    def test_selects_the_shortest_articles_above_the_limit(self, db_session: Session) -> None:
        """外れ値（最長記事）ではなく、上限を超える中で最も短い記事から選ぶ。"""
        _article_row(db_session, slug="too-short", body="x" * 100)
        _article_row(db_session, slug="shortest", body="x" * 300)
        _article_row(db_session, slug="middle", body="x" * 500)
        _article_row(db_session, slug="outlier", body="x" * 900)

        articles = _load_measurement_bodies(db_session, limit=200, count=2)

        assert [a.canonical_url for a in articles] == [
            "https://example.com/shortest",
            "https://example.com/middle",
        ]

    def test_excludes_articles_exactly_at_the_limit(self, db_session: Session) -> None:
        """`body[:limit]` はちょうど上限の記事を切り捨てないため、比較対象から外す。"""
        _article_row(db_session, slug="exact", body="x" * 200)
        _article_row(db_session, slug="over", body="x" * 201)

        articles = _load_measurement_bodies(db_session, limit=200, count=5)

        assert [a.canonical_url for a in articles] == ["https://example.com/over"]

    def test_skips_articles_without_a_body(self, db_session: Session) -> None:
        _article_row(db_session, slug="no-body", body=None)
        _article_row(db_session, slug="has-body", body="x" * 300)

        articles = _load_measurement_bodies(db_session, limit=200, count=5)

        assert [a.canonical_url for a in articles] == ["https://example.com/has-body"]

    def test_limits_to_the_requested_count(self, db_session: Session) -> None:
        for index in range(3):
            _article_row(db_session, slug=f"a{index}", body="x" * (300 + index))

        articles = _load_measurement_bodies(db_session, limit=200, count=1)

        assert len(articles) == 1


class TestCompareArticle:
    def test_compares_truncated_and_full_versions_on_success(self) -> None:
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        result = _compare_article(provider, article, limit=200, index=1, total=1)

        assert result.failure is None
        assert result.impact is not None
        assert result.impact.domain_matches is True
        assert result.impact.category_matches is False

    def test_passes_the_truncated_body_first_then_the_full_body(self) -> None:
        """1 回目は切り捨てた本文、2 回目は全文が渡ることを確かめる。"""
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        _compare_article(provider, article, limit=200, index=1, total=1)

        assert len(provider.calls[0]["untrusted_content"]) == 200
        assert len(provider.calls[1]["untrusted_content"]) == 300

    def test_records_a_failure_when_the_truncated_call_fails(self) -> None:
        """切り捨て版が失敗したら全文版は呼ばず、例外の型とメッセージを記録する。"""
        provider = FakeLLMProvider([LLMError("truncated 失敗")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        result = _compare_article(provider, article, limit=200, index=1, total=1)

        assert result.impact is None
        assert result.failure == ComparisonFailure(
            exception_type="LLMError", message="truncated 失敗"
        )
        assert len(provider.calls) == 1

    def test_records_a_failure_when_the_full_call_fails(self) -> None:
        """全文版だけが失敗した場合も、比較不能として記録する。"""
        provider = FakeLLMProvider([_RESPONSE_A, LLMError("full 失敗")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        result = _compare_article(provider, article, limit=200, index=1, total=1)

        assert result.impact is None
        assert result.failure == ComparisonFailure(exception_type="LLMError", message="full 失敗")

    def test_prints_progress_with_the_article_label(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/progress", body="x" * 300)

        _compare_article(provider, article, limit=200, index=2, total=3)

        captured = capsys.readouterr()
        assert "[2/3]" in captured.err
        assert "https://example.com/progress" in captured.err

    def test_reraises_tool_use_detected_error_from_the_truncated_call(self) -> None:
        """隔離破りの検知シグナルは比較不能として記録せず、そのまま送出して計測を止める
        （ADR 0002）。"""
        provider = FakeLLMProvider([LLMToolUseDetectedError("ツール使用を検知")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        with pytest.raises(LLMToolUseDetectedError):
            _compare_article(provider, article, limit=200, index=1, total=1)

    def test_reraises_managed_policy_detected_error_from_the_truncated_call(self) -> None:
        provider = FakeLLMProvider([LLMManagedPolicyDetectedError("管理者ポリシーを検知")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        with pytest.raises(LLMManagedPolicyDetectedError):
            _compare_article(provider, article, limit=200, index=1, total=1)

    def test_reraises_tool_use_detected_error_from_the_full_call(self) -> None:
        """切り捨て版が成功しても、全文版の検知は同じく握りつぶさない。"""
        provider = FakeLLMProvider([_RESPONSE_A, LLMToolUseDetectedError("ツール使用を検知")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        with pytest.raises(LLMToolUseDetectedError):
            _compare_article(provider, article, limit=200, index=1, total=1)


class TestRenderText:
    def test_includes_the_comparison_and_both_texts(self) -> None:
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)
        result = _compare_article(provider, article, limit=200, index=1, total=1)

        rendered = _render_text([result])

        assert "https://example.com/a" in rendered
        assert "要約A" in rendered
        assert "要約B" in rendered
        assert "題A" in rendered
        assert "題B" in rendered

    def test_marks_a_failed_article_as_incomparable(self) -> None:
        results = [
            ArticleComparisonResult(
                article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
                limit=200,
                impact=None,
                failure=ComparisonFailure(exception_type="LLMTimeoutError", message="timeout"),
            )
        ]

        rendered = _render_text(results)

        assert "比較不能" in rendered
        assert "LLMTimeoutError" in rendered
        assert "timeout" in rendered

    def test_includes_overall_summary_counts(self) -> None:
        """成功 1 件・失敗 1 件のときの全体集計を確かめる。"""
        succeeded = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
            limit=200,
            impact=compare_analyses(_fake_analysis(), _fake_analysis()),
            failure=None,
        )
        failed = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/b", body="x" * 300),
            limit=200,
            impact=None,
            failure=ComparisonFailure(exception_type="LLMError", message="失敗"),
        )

        rendered = _render_text([succeeded, failed])

        assert "記事 2 件中 比較 1 件 / 失敗 1 件" in rendered


class TestRenderJson:
    def test_includes_articles_and_summary_sections(self) -> None:
        result = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
            limit=200,
            impact=compare_analyses(_fake_analysis(), _fake_analysis()),
            failure=None,
        )

        parsed = json.loads(_render_json([result]))

        assert parsed["articles"][0]["canonical_url"] == "https://example.com/a"
        assert parsed["articles"][0]["body_length"] == 300
        assert parsed["articles"][0]["limit"] == 200
        assert parsed["articles"][0]["failure"] is None
        assert parsed["articles"][0]["impact"]["domain_matches"] is True
        assert parsed["summary"]["compared_count"] == 1
        assert parsed["summary"]["failed_count"] == 0

    def test_represents_a_failure_without_an_impact(self) -> None:
        result = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
            limit=200,
            impact=None,
            failure=ComparisonFailure(exception_type="LLMTimeoutError", message="timeout"),
        )

        parsed = json.loads(_render_json([result]))

        assert parsed["articles"][0]["impact"] is None
        assert parsed["articles"][0]["failure"] == {
            "exception_type": "LLMTimeoutError",
            "message": "timeout",
        }
        assert parsed["summary"]["compared_count"] == 0
        assert parsed["summary"]["failed_count"] == 1


class TestParseArgsControl:
    def test_control_flag_defaults_to_false(self) -> None:
        """既定は通常モード。切り捨てが結果へ与える影響を測るのが本来の目的のため。"""
        args = _parse_args([])

        assert args.control is False

    def test_control_flag_can_be_set(self) -> None:
        args = _parse_args(["--control"])

        assert args.control is True


class TestCompareArticleControlMode:
    def test_passes_the_full_body_twice(self) -> None:
        """対照モードでは切り捨てず、全文を2回そのまま渡す。

        LLM は同じ入力でも実行ごとに出力が揺れるため、これが実行ごとのばらつきの
        ベースラインになる（通常モードとの比較のため同じ土俵に立たせる）。
        """
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        _compare_article(provider, article, limit=200, index=1, total=1, control=True)

        assert len(provider.calls[0]["untrusted_content"]) == 300
        assert len(provider.calls[1]["untrusted_content"]) == 300
        assert provider.calls[0]["untrusted_content"] == provider.calls[1]["untrusted_content"]

    def test_still_compares_and_records_an_impact(self) -> None:
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        result = _compare_article(provider, article, limit=200, index=1, total=1, control=True)

        assert result.failure is None
        assert result.impact is not None
        assert result.impact.category_matches is False

    def test_records_a_failure_when_the_first_call_fails(self) -> None:
        provider = FakeLLMProvider([LLMError("失敗")])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        result = _compare_article(provider, article, limit=200, index=1, total=1, control=True)

        assert result.impact is None
        assert result.failure == ComparisonFailure(exception_type="LLMError", message="失敗")

    def test_progress_messages_mention_full_text_variants(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """進捗表示は通常モードの「切り捨て版」ではなく全文版であることが分かる文言にする。"""
        provider = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
        article = MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300)

        _compare_article(provider, article, limit=200, index=1, total=1, control=True)

        captured = capsys.readouterr()
        assert "切り捨て版" not in captured.err
        assert "全文版(1回目)" in captured.err
        assert "全文版(2回目)" in captured.err


class TestRenderTextControlMode:
    def test_normal_mode_heading_mentions_truncation(self) -> None:
        result = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
            limit=200,
            impact=compare_analyses(_fake_analysis(), _fake_analysis()),
            failure=None,
        )

        rendered = _render_text([result], control=False)

        assert "通常モード" in rendered
        assert "切り捨て版" in rendered
        assert "対照モード" not in rendered

    def test_control_mode_heading_mentions_the_baseline(self) -> None:
        result = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
            limit=200,
            impact=compare_analyses(_fake_analysis(), _fake_analysis()),
            failure=None,
        )

        rendered = _render_text([result], control=True)

        assert "対照モード" in rendered
        assert "全文版(1回目)" in rendered
        assert "全文版(2回目)" in rendered
        assert "切り捨て版" not in rendered


class TestRenderJsonControlMode:
    def test_includes_the_control_flag(self) -> None:
        result = ArticleComparisonResult(
            article=MeasurementArticle(canonical_url="https://example.com/a", body="x" * 300),
            limit=200,
            impact=compare_analyses(_fake_analysis(), _fake_analysis()),
            failure=None,
        )

        normal_parsed = json.loads(_render_json([result], control=False))
        control_parsed = json.loads(_render_json([result], control=True))

        assert normal_parsed["control"] is False
        assert control_parsed["control"] is True


@pytest.fixture
def stubbed_session(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Session:
    """`read_only_session` をテスト用セッションへ差し替える。

    `main()` は本番 DB を読み取り専用で参照するが、ここではテストごとにロールバックされる
    `db_session` を使う（`test_measure_cli.py` の流儀に合わせる）。
    """

    @contextmanager
    def fake_session() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(cli, "read_only_session", fake_session)
    return db_session


@pytest.fixture
def stubbed_provider(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    """`ClaudeCliProvider` を `FakeLLMProvider` へ差し替える。実際の CLI は呼ばない。"""
    fake = FakeLLMProvider([_RESPONSE_A, _RESPONSE_B])
    monkeypatch.setattr(cli, "ClaudeCliProvider", lambda settings: fake)
    return fake


class TestMain:
    def test_returns_1_and_prints_to_stderr_when_no_articles(
        self, stubbed_session: Session, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli.main(["--limit", "200"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "測れる本文がありません" in captured.err
        assert captured.out == ""

    def test_returns_0_on_success(
        self, stubbed_session: Session, stubbed_provider: FakeLLMProvider
    ) -> None:
        _article_row(stubbed_session, slug="a", body="x" * 300)

        exit_code = cli.main(["--limit", "200", "--articles", "1"])

        assert exit_code == 0

    def test_prints_text_by_default(
        self,
        stubbed_session: Session,
        stubbed_provider: FakeLLMProvider,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _article_row(stubbed_session, slug="a", body="x" * 300)

        cli.main(["--limit", "200", "--articles", "1"])

        assert "記事:" in capsys.readouterr().out

    def test_prints_json_with_flag(
        self,
        stubbed_session: Session,
        stubbed_provider: FakeLLMProvider,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _article_row(stubbed_session, slug="a", body="x" * 300)

        cli.main(["--limit", "200", "--articles", "1", "--json"])

        parsed = json.loads(capsys.readouterr().out)
        assert parsed["articles"][0]["canonical_url"] == "https://example.com/a"

    def test_control_flag_propagates_to_the_comparison(
        self, stubbed_session: Session, stubbed_provider: FakeLLMProvider
    ) -> None:
        """`--control` が main() 経由で伝播し、切り捨てずに全文を2回渡すことを確かめる。"""
        _article_row(stubbed_session, slug="a", body="x" * 300)

        cli.main(["--limit", "200", "--articles", "1", "--control"])

        assert len(stubbed_provider.calls[0]["untrusted_content"]) == 300
        assert len(stubbed_provider.calls[1]["untrusted_content"]) == 300

    def test_without_control_flag_truncates_the_first_call(
        self, stubbed_session: Session, stubbed_provider: FakeLLMProvider
    ) -> None:
        _article_row(stubbed_session, slug="a", body="x" * 300)

        cli.main(["--limit", "200", "--articles", "1"])

        assert len(stubbed_provider.calls[0]["untrusted_content"]) == 200
