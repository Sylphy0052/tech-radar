"""記事解析の出力スキーマ（`PROJECT_SPEC.md` §9）。

LLM の応答をこのスキーマで検証する。想定外の形なら失敗させ、
壊れたデータが DB へ入らないようにする。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from techradar.db.enums import ContentType, Difficulty

MAX_TOPICS = 8
MAX_TECHNOLOGIES = 8
MAX_SUMMARY_LENGTH = 400


class ArticleAnalysis(BaseModel):
    """LLM が生成する記事の構造化データ。

    原文言語を問わず要約は日本語で作る（`PROJECT_SPEC.md` §16）。
    """

    translated_title: str | None = Field(
        default=None,
        description="日本語タイトル。原文が日本語なら null。",
    )
    summary_ja: str = Field(
        description="日本語の要約。原文の言語を問わず日本語で書く。",
        min_length=1,
        max_length=MAX_SUMMARY_LENGTH,
    )
    domain: str = Field(description="大分類。例: Generative AI, Web Frontend", min_length=1)
    category: str = Field(description="中分類。例: Agentic Engineering", min_length=1)
    topics: list[str] = Field(
        default_factory=list,
        description="記事の主題。例: MCP, Context Engineering",
        max_length=MAX_TOPICS,
    )
    technologies: list[str] = Field(
        default_factory=list,
        description="登場する製品・OSS 名。例: Claude Code, PostgreSQL",
        max_length=MAX_TECHNOLOGIES,
    )
    content_type: ContentType = Field(description="記事の性質")
    difficulty: Difficulty = Field(description="読者に求められる前提知識の程度")
    technical_quality: float = Field(
        description="技術的な質。0.0〜1.0。具体性・検証の有無・情報の新しさで判断する。",
        ge=0.0,
        le=1.0,
    )

    @field_validator("topics", "technologies", mode="after")
    @classmethod
    def _strip_and_drop_empty(cls, values: list[str]) -> list[str]:
        """空文字と重複を除く。LLM が空要素を混ぜることがある。"""
        seen: list[str] = []
        for value in values:
            stripped = value.strip()
            if stripped and stripped not in seen:
                seen.append(stripped)
        return seen

    @field_validator("translated_title", mode="after")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        """空文字は「訳が不要」と同じ意味なので None に寄せる。"""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None
