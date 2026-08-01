"""解説記事の独自価値判定（`PROJECT_SPEC.md` §17）。

同一ニュースについて公式記事と解説記事があるとき、原則は公式記事を代表として
残すことだが、解説記事に独自検証・独自コード・実測値がある場合は別記事として
残す。この判別は機械的な規則では決められないため LLM に委ねる。
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from techradar.db.enums import JobType
from techradar.llm import LLMProvider, complete_json_with_retry
from techradar.llm.errors import LLMError

OPERATION = JobType.DEDUPLICATE_ARTICLES.value

# LLM へ渡す本文の長さ。独自検証・実測値の有無は冒頭で判断できることが多く、
# 全文を渡すとトークンが嵩む（`analysis/service.py` の方針に合わせる）。
MAX_JUDGE_BODY_CHARACTERS = 12000

# タイトルも外部由来の値なので上限を設ける。
MAX_JUDGE_TITLE_CHARACTERS = 300

# LLM が生成する判定理由の長さ上限。ログ・表示で扱いやすい範囲に収める。
MAX_REASON_LENGTH = 400

# 表示や保存で扱いにくい制御文字。LLM 出力に紛れることがあるため落とす
# （`analysis/schema.py` と同じパターン）。
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: str) -> str:
    """制御文字を除いて前後の空白を落とす。"""
    return _CONTROL_CHARACTERS.sub("", value).strip()


class UniqueValueJudgment(BaseModel):
    """独自価値判定の出力スキーマ。"""

    has_unique_value: bool = Field(
        description="公式発表には無い独自検証・独自コード・実測値が含まれるか"
    )
    reason: str = Field(
        description="判定理由。日本語で 1〜2 文。",
        min_length=1,
        max_length=MAX_REASON_LENGTH,
    )

    @field_validator("reason", mode="after")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        """制御文字を落とす。"""
        return _clean(value)


UNIQUE_VALUE_INSTRUCTION = """技術記事を読み、この記事に独自の価値があるかを判定してください。

「独自の価値がある」とは、公式発表（公式ブログ・リリースノート・ドキュメント）
には無い、次のいずれかが記事に含まれることを指します。

- 執筆者自身が実施した検証・実験の記録
- 執筆者が書いた独自のコード（公式サンプルの転載ではないもの）
- 執筆者が測定した実測値・ベンチマーク結果

単なる公式発表の要約・翻訳・転載には独自の価値はありません。

判定結果を has_unique_value（真偽値）、判断理由を reason（日本語で 1〜2 文）
として、指定された JSON で出力してください。出力は JSON のみとし、
説明文やコードフェンスを付けないでください。"""


def judge_unique_value(
    provider: LLMProvider,
    *,
    title: str,
    body: str,
    job_id: uuid.UUID | None = None,
    session: Session | None = None,
    article_id: uuid.UUID | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """記事に独自価値があるかを LLM に判定させる。

    LLM 呼び出し（リトライを含む）が最終的に失敗した場合は例外を伝播させず
    False を返す。`PROJECT_SPEC.md` §17 の原則は「公式記事を優先する」であり、
    判定不能な記事を安全側（重複として畳む）に倒すほうが、判定不能な記事を
    すべて別記事として残して一覧のノイズを増やすより望ましいため。
    失敗理由は `complete_json_with_retry` が `operation_logs` へ記録済み。
    """
    try:
        completion = complete_json_with_retry(
            provider,
            instruction=UNIQUE_VALUE_INSTRUCTION,
            untrusted_content=_judge_input(title, body),
            schema=UniqueValueJudgment,
            operation=OPERATION,
            session=session,
            article_id=article_id,
            job_id=job_id,
            sleep=sleep,
        )
    except LLMError:
        return False

    judgment = UniqueValueJudgment.model_validate(completion.data)
    return judgment.has_unique_value


def _judge_input(title: str, body: str) -> str:
    """LLM へ渡す非信頼テキストを組み立てる。

    タイトルだけでは主題が読み取りにくいため本文と合わせる
    （`analysis/service.py` の `_analysis_input` に合わせる）。
    """
    truncated_title = title[:MAX_JUDGE_TITLE_CHARACTERS]
    truncated_body = body[:MAX_JUDGE_BODY_CHARACTERS]
    return f"タイトル: {truncated_title}\n\n本文:\n{truncated_body}".strip()
