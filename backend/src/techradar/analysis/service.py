"""記事解析パイプライン（`PROJECT_SPEC.md` §9, §16, §23 Phase 2）。

言語判定 → LLM による構造化抽出 → DB 保存 の順に処理する。

同じ本文を二度解析しない（§24 コスト管理）。判定は
`articles.analyzed_body_hash` と現在の `body_hash` の比較で行う。
`summary_ja` の有無だけで判定すると、本文が更新された記事の要約が古いまま残る。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from techradar.analysis.language import resolve_language
from techradar.analysis.prompt import ANALYSIS_INSTRUCTION
from techradar.analysis.schema import ArticleAnalysis
from techradar.db import Article
from techradar.db.enums import JobStatus, JobType
from techradar.llm import LLMProvider, complete_json_with_retry

OPERATION = JobType.ANALYZE_ARTICLE.value

# LLM へ渡す本文の長さ。全文を渡すとトークンが嵩むうえ、
# 要約と分類には冒頭で足りることが多い。
MAX_ANALYSIS_BODY_CHARACTERS = 12000

# タイトルも外部由来なので上限を設ける。
MAX_ANALYSIS_TITLE_CHARACTERS = 300


@dataclass(frozen=True)
class AnalysisResult:
    """解析結果。

    `analyzed` が False なら、本文が変わっておらず再解析しなかったことを表す。
    """

    article: Article
    analyzed: bool


def needs_analysis(article: Article) -> bool:
    """解析し直す必要があるかを判定する。"""
    if article.summary_ja is None:
        return True
    return article.analyzed_body_hash != article.body_hash


def analyze_article(
    session: Session,
    provider: LLMProvider,
    article: Article,
    *,
    job_id: uuid.UUID | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AnalysisResult:
    """記事を解析して構造化データを保存する。

    本文が変わっていなければ LLM を呼ばない。
    失敗した場合は `failed` へ遷移させ、理由は `operation_logs` に残る。

    Args:
        job_id: 呼び出し元のジョブ。`operation_logs` から実行を辿れるようにする。
        sleep: リトライのバックオフ待機。テストから差し替える。

    Raises:
        LLMError: リトライしても解析できなかった場合。
    """
    if not needs_analysis(article):
        return AnalysisResult(article=article, analyzed=False)

    # 言語判定は LLM を使わない（§24 コスト管理）。
    # 前回の解析で確定した値を宣言として渡さないよう、元の宣言値は使わず
    # 本文から判定し直す（`resolve_language` は本文推定を優先する）。
    article.language = resolve_language(declared=article.language, body=article.body or "")
    article.analysis_status = JobStatus.ANALYZING
    session.flush()

    try:
        completion = complete_json_with_retry(
            provider,
            instruction=ANALYSIS_INSTRUCTION,
            untrusted_content=_analysis_input(article),
            schema=ArticleAnalysis,
            operation=OPERATION,
            session=session,
            article_id=article.id,
            job_id=job_id,
            sleep=sleep,
        )
        _apply(article, ArticleAnalysis.model_validate(completion.data))
    except Exception:
        # LLM 失敗だけでなく、保存時の失敗でも `analyzing` のまま残さない。
        article.analysis_status = JobStatus.FAILED
        session.flush()
        raise

    session.flush()
    return AnalysisResult(article=article, analyzed=True)


def _analysis_input(article: Article) -> str:
    """LLM へ渡すテキストを組み立てる。

    タイトルと本文を合わせる。本文だけだと主題が読み取りにくい。
    """
    title = article.title[:MAX_ANALYSIS_TITLE_CHARACTERS]
    body = (article.body or "")[:MAX_ANALYSIS_BODY_CHARACTERS]
    return f"タイトル: {title}\n\n本文:\n{body}".strip()


def _apply(article: Article, analysis: ArticleAnalysis) -> None:
    """解析結果を記事へ反映する。"""
    article.translated_title = _resolve_translated_title(article, analysis)
    article.summary_ja = analysis.summary_ja
    article.domain = analysis.domain
    article.category = analysis.category
    article.topics = analysis.topics
    article.technologies = analysis.technologies
    article.content_type = analysis.content_type
    article.difficulty = analysis.difficulty
    article.technical_quality = analysis.technical_quality
    article.analyzed_body_hash = article.body_hash
    article.analysis_status = JobStatus.COMPLETED


def _resolve_translated_title(article: Article, analysis: ArticleAnalysis) -> str | None:
    """日本語タイトルを決める。

    原文が日本語なら訳は不要なので None にする。LLM が原文をそのまま
    返してくることがあるため、一致する場合も None に寄せる。
    """
    if article.language == "ja":
        return None
    if analysis.translated_title == article.title:
        return None
    return analysis.translated_title
