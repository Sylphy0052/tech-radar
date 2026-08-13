"""レビューで判明した誤判定・破損経路を固定するテスト。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from techradar.analysis import analyze_article
from techradar.analysis.language import normalize_language_tag, resolve_language
from techradar.analysis.prompt import ANALYSIS_INSTRUCTION
from techradar.analysis.schema import MAX_LABEL_LENGTH, MAX_SUMMARY_LENGTH, ArticleAnalysis
from techradar.db import Article, OperationLog
from techradar.db.enums import JobStatus, JobType
from techradar.llm import FakeLLMProvider
from techradar.llm.errors import LLMInvalidResponseError
from tests.test_analysis_service import (
    ENGLISH_BODY,
    JAPANESE_BODY,
    VALID_ANALYSIS,
    make_article,
    no_sleep,
)


class TestLanguagePrefersDetection:
    def test_detects_english_even_when_declared_japanese(self):
        # Arrange / Act — `<html lang="ja">` のまま放置された英語記事。
        # 宣言を優先すると翻訳タイトルが作られなくなる
        resolved = resolve_language(declared="ja", body=ENGLISH_BODY)

        # Assert
        assert resolved == "en"

    def test_detects_japanese_even_when_declared_english(self):
        # Arrange / Act — `lang="en"` はテンプレート初期値として最も多い
        resolved = resolve_language(declared="en", body=JAPANESE_BODY)

        # Assert
        assert resolved == "ja"

    def test_falls_back_to_declared_only_when_detection_fails(self):
        # Arrange / Act — 本文が短く推定できない場合のみ宣言値を使う
        assert resolve_language(declared="fr", body="ok") == "fr"

    @pytest.mark.parametrize("declared", ["x-default", "und", "zxx", "1234", "englishlanguage"])
    def test_rejects_meaningless_declared_values(self, declared: str):
        # Arrange / Act / Assert — `x-default` から `x` を作ってしまわないこと
        assert resolve_language(declared=declared, body="ok") is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("ja", "ja"), ("JA-jp", "ja"), ("x-default", None), ("und", None), ("12", None)],
    )
    def test_normalize_rejects_invalid_codes(self, raw: str, expected: str | None):
        # Arrange / Act / Assert
        assert normalize_language_tag(raw) == expected


class TestLanguageOnReanalysis:
    def test_redetects_language_when_the_body_language_changes(self, db_session: Session):
        # Arrange — 1 回目で確定した言語が 2 回目の判定を縛らないこと
        article = make_article(db_session, body=ENGLISH_BODY)
        provider = FakeLLMProvider([VALID_ANALYSIS])
        analyze_article(db_session, provider, article, sleep=no_sleep)
        assert article.language == "en"

        # Act
        article.body = JAPANESE_BODY
        article.body_hash = "hash-2"
        db_session.flush()
        analyze_article(db_session, provider, article, sleep=no_sleep)

        # Assert
        assert article.language == "ja"

    def test_generates_translated_title_for_a_mislabelled_english_article(
        self, db_session: Session
    ):
        # Arrange — `lang="ja"` だが本文は英語。受入基準
        # 「英語記事から日本語タイトルが生成される」を守れるか
        article = make_article(db_session, language="ja", body=ENGLISH_BODY)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        # Act
        analyze_article(db_session, provider, article, sleep=no_sleep)

        # Assert
        assert article.language == "en"
        assert article.translated_title == "MCP サーバー実装ガイド"


class TestFailureLeavesNoStuckState:
    def test_records_the_reason_for_invalid_json(self, db_session: Session):
        # Arrange — 受入基準は「不正 JSON のとき failed へ遷移し理由が記録される」
        article = make_article(db_session)
        provider = FakeLLMProvider(["not json at all"])

        # Act
        with pytest.raises(LLMInvalidResponseError):
            analyze_article(db_session, provider, article, sleep=no_sleep)

        # Assert
        log = db_session.scalars(
            select(OperationLog).where(OperationLog.article_id == article.id)
        ).one()
        assert log.status == "failed"
        assert log.error_reason == "llm_invalid_response"
        assert article.analysis_status == JobStatus.FAILED

    def test_session_stays_usable_after_a_failure(self, db_session: Session):
        # Arrange — 失敗後に session が壊れていると以降の更新が黙って消える
        article = make_article(db_session)
        provider = FakeLLMProvider(["not json"])

        # Act
        with pytest.raises(LLMInvalidResponseError):
            analyze_article(db_session, provider, article, sleep=no_sleep)

        # Assert — 続けて読み書きできる
        db_session.refresh(article)
        article.title = "更新後のタイトル"
        db_session.flush()
        assert article.title == "更新後のタイトル"

    def test_marks_failed_when_saving_the_result_fails(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — LLM は成功するが保存で落ちる経路。
        # `analyzing` のまま残さないこと
        article = make_article(db_session)
        provider = FakeLLMProvider([VALID_ANALYSIS])

        def _explode(article: Article, analysis: ArticleAnalysis) -> None:
            del article, analysis
            message = "boom"
            raise RuntimeError(message)

        monkeypatch.setattr("techradar.analysis.service._apply", _explode)

        # Act / Assert
        with pytest.raises(RuntimeError, match="boom"):
            analyze_article(db_session, provider, article, sleep=no_sleep)

        # Assert
        assert article.analysis_status == JobStatus.FAILED


class TestOperationLogging:
    def test_links_the_log_to_the_job(self, db_session: Session):
        # Arrange — §24 可観測性。どのジョブの実行かを辿れるようにする
        from techradar.db import Job

        job = Job(type=JobType.ANALYZE_ARTICLE, payload={})
        db_session.add(job)
        db_session.flush()
        article = make_article(db_session)

        # Act
        analyze_article(
            db_session, FakeLLMProvider([VALID_ANALYSIS]), article, job_id=job.id, sleep=no_sleep
        )

        # Assert
        log = db_session.scalars(
            select(OperationLog).where(OperationLog.article_id == article.id)
        ).one()
        assert log.job_id == job.id

    def test_uses_the_job_type_as_the_operation_name(self, db_session: Session):
        # Arrange — 文字列の二重定義を避ける
        from techradar.analysis.service import OPERATION

        # Act / Assert
        assert OPERATION == JobType.ANALYZE_ARTICLE.value


class TestAnalysisStatusLifecycle:
    def test_new_articles_start_as_pending(self, db_session: Session):
        # Arrange — NULL だと「未解析」か「不明」かを区別できない
        article = Article(
            canonical_url=f"https://example.com/{uuid.uuid4().hex[:8]}",
            original_url="https://example.com/a",
            title="T",
            source_domain="example.com",
            analysis_status=JobStatus.PENDING,
        )
        db_session.add(article)
        db_session.flush()

        # Act / Assert
        assert article.analysis_status == JobStatus.PENDING


class TestSchemaLimits:
    def test_truncates_overlong_topic_elements(self):
        # Arrange / Act — 本文由来の巨大文字列がそのまま DB へ入らないこと
        analysis = ArticleAnalysis.model_validate({**VALID_ANALYSIS, "topics": ["あ" * 500]})

        # Assert
        assert len(analysis.topics[0]) == MAX_LABEL_LENGTH

    @pytest.mark.parametrize("field", ["domain", "category"])
    def test_rejects_overlong_labels(self, field: str):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="at most"):
            ArticleAnalysis.model_validate({**VALID_ANALYSIS, field: "x" * 200})

    def test_strips_control_characters(self):
        # Arrange / Act
        analysis = ArticleAnalysis.model_validate(
            {
                **VALID_ANALYSIS,
                "summary_ja": "要約\x00に制御文字\x1fが混ざる",
                "topics": ["MCP\x07"],
            }
        )

        # Assert
        assert "\x00" not in analysis.summary_ja
        assert "\x1f" not in analysis.summary_ja
        assert analysis.topics == ["MCP"]

    def test_truncates_an_overlong_summary(self):
        # Arrange / Act — 発表内容の列挙が多い記事では LLM が上限を超える要約を返す
        # (Issue #86)。落として記事を未解析のまま残すより、切って通すほうが被害が小さい
        analysis = ArticleAnalysis.model_validate(
            {**VALID_ANALYSIS, "summary_ja": "あ" * (MAX_SUMMARY_LENGTH + 1)}
        )

        # Assert
        assert len(analysis.summary_ja) == MAX_SUMMARY_LENGTH

    def test_keeps_a_summary_at_the_limit(self):
        # Arrange / Act — 上限ちょうどは切り詰めの対象にしない (境界)
        summary = "あ" * MAX_SUMMARY_LENGTH
        analysis = ArticleAnalysis.model_validate({**VALID_ANALYSIS, "summary_ja": summary})

        # Assert
        assert analysis.summary_ja == summary

    def test_truncates_after_removing_control_characters(self):
        # Arrange / Act — 制御文字を除く前に切ると、除去後の長さが上限を下回る
        summary = "あ" * MAX_SUMMARY_LENGTH + "\x00" * 10
        analysis = ArticleAnalysis.model_validate({**VALID_ANALYSIS, "summary_ja": summary})

        # Assert
        assert len(analysis.summary_ja) == MAX_SUMMARY_LENGTH

    def test_rejects_an_overlong_translated_title(self):
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="at most"):
            ArticleAnalysis.model_validate({**VALID_ANALYSIS, "translated_title": "あ" * 400})

    def test_accepts_an_explicit_null_translated_title(self):
        # Arrange / Act — 日本語記事では LLM が null を返す想定
        analysis = ArticleAnalysis.model_validate({**VALID_ANALYSIS, "translated_title": None})

        # Assert
        assert analysis.translated_title is None


class TestOverlongSummaryReachesTheArticle:
    def test_saves_a_truncated_summary_instead_of_failing(self, db_session: Session):
        # Arrange — 受入基準は「上限を超える要約でも記事が解析済みになる」(Issue #86)。
        # スキーマ単体で切り詰めが効いても、パイプラインの別の層で長さを見ていれば
        # 記事は failed のまま残る。`analyze_article` を通して確かめる
        article = make_article(db_session)
        provider = FakeLLMProvider(
            [{**VALID_ANALYSIS, "summary_ja": "あ" * (MAX_SUMMARY_LENGTH + 47)}]
        )

        # Act
        result = analyze_article(db_session, provider, article, sleep=no_sleep)

        # Assert
        assert result.analyzed is True
        assert article.analysis_status == JobStatus.COMPLETED
        assert article.summary_ja is not None
        assert len(article.summary_ja) == MAX_SUMMARY_LENGTH


class TestAnalysisInstruction:
    def test_states_the_summary_length_limit(self):
        # Arrange / Act / Assert — スキーマ側の切り詰めは安全網であって、
        # 常用させたいわけではない。プロンプトから上限が消えたらここで落とす (Issue #86)
        assert str(MAX_SUMMARY_LENGTH) in ANALYSIS_INSTRUCTION
