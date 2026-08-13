"""記事解析の出力スキーマ（`PROJECT_SPEC.md` §9）。

LLM の応答をこのスキーマで検証する。想定外の形なら失敗させ、
壊れたデータが DB へ入らないようにする。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from techradar.db.enums import ContentType, Difficulty

MAX_TOPICS = 8
MAX_TECHNOLOGIES = 8
MAX_SUMMARY_LENGTH = 400
MAX_LABEL_LENGTH = 80
MAX_TITLE_LENGTH = 300

# 表示や保存で扱いにくい制御文字。LLM 出力に紛れることがあるため落とす。
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: str) -> str:
    """制御文字を除いて前後の空白を落とす。"""
    return _CONTROL_CHARACTERS.sub("", value).strip()


class ArticleAnalysis(BaseModel):
    """LLM が生成する記事の構造化データ。

    原文言語を問わず要約は日本語で作る（`PROJECT_SPEC.md` §16）。
    """

    translated_title: str | None = Field(
        default=None,
        description="日本語タイトル。原文が日本語なら null。",
        max_length=MAX_TITLE_LENGTH,
    )
    summary_ja: str = Field(
        description="日本語の要約。原文の言語を問わず日本語で書く。",
        min_length=1,
    )
    domain: str = Field(
        description="大分類。例: Generative AI, Web Frontend",
        min_length=1,
        max_length=MAX_LABEL_LENGTH,
    )
    category: str = Field(
        description="中分類。例: Agentic Engineering",
        min_length=1,
        max_length=MAX_LABEL_LENGTH,
    )
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

    @field_validator("domain", "category", mode="after")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        """制御文字を落とす。"""
        return _clean(value)

    @field_validator("summary_ja", mode="after")
    @classmethod
    def _clean_and_truncate_summary(cls, value: str) -> str:
        """制御文字を落とし、上限を超えた分を切る。

        発表内容の列挙が多い記事では、LLM が上限を超える要約を返すことがある
        （Issue #86）。`max_length` で弾くと同じ入力に対して再試行しても同じ長さが
        返り、記事が未解析のまま残る。要約は表示用途のため、末尾が欠けても
        解析結果ごと失うよりは被害が小さい。

        切るのは制御文字を除いた後にする。先に切ると、除去したぶんだけ
        上限を下回った要約になる。
        """
        return _clean(value)[:MAX_SUMMARY_LENGTH]

    @field_validator("topics", "technologies", mode="after")
    @classmethod
    def _strip_and_drop_empty(cls, values: list[str]) -> list[str]:
        """空文字・重複・長すぎる要素を除く。LLM が混ぜることがある。"""
        seen: list[str] = []
        for value in values:
            cleaned = _clean(value)[:MAX_LABEL_LENGTH]
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @field_validator("translated_title", mode="after")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        """空文字は「訳が不要」と同じ意味なので None に寄せる。"""
        if value is None:
            return None
        return _clean(value) or None
