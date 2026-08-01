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
from techradar.db.enums import JobStatus
from techradar.llm import LLMError, LLMProvider, complete_json_with_retry

OPERATION = "analyze_article"

# LLM へ渡す本文の長さ。全文を渡すとトークンが嵩むうえ、
# 要約と分類には冒頭で足りることが多い。
MAX_ANALYSIS_BODY_CHARACTERS = 12000


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
    user_id: uuid.UUID | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AnalysisResult:
    """記事を解析して構造化データを保存する。

    本文が変わっていなければ LLM を呼ばない。
    失敗した場合は `failed` へ遷移させ、理由は `operation_logs` に残る。

    Raises:
        LLMError: リトライしても解析できなかった場合。
    """
    del user_id  # 現状は単一ユーザーのため未使用。マルチユーザー化時に記録へ加える。

    if not needs_analysis(article):
        return AnalysisResult(article=article, analyzed=False)

    # 言語判定は LLM を使わない（§24 コスト管理）。
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
            sleep=sleep,
        )
    except LLMError:
        article.analysis_status = JobStatus.FAILED
        session.flush()
        raise

    _apply(article, ArticleAnalysis.model_validate(completion.data))
    session.flush()
    return AnalysisResult(article=article, analyzed=True)


def _analysis_input(article: Article) -> str:
    """LLM へ渡すテキストを組み立てる。

    タイトルと本文を合わせる。本文だけだと主題が読み取りにくい。
    """
    body = (article.body or "")[:MAX_ANALYSIS_BODY_CHARACTERS]
    return f"タイトル: {article.title}\n\n本文:\n{body}".strip()


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
