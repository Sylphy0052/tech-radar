"""原文言語の判定（`PROJECT_SPEC.md` §16）。

LLM を使わず軽量ライブラリで判定する。言語判定のためだけに LLM を呼ぶのは
コストに見合わない（`PROJECT_SPEC.md` §24 コスト管理）。

`<html lang>` が信頼できる場合はそれを優先する。テンプレートの初期値が
そのまま残っているサイトもあるため、本文からの推定と食い違う場合は推定を採る。
"""

from __future__ import annotations

import py3langid

# 判定に使う本文の長さ。全文を渡しても精度は上がらず時間だけ延びる。
DETECTION_SAMPLE_LENGTH = 2000

# これ未満のテキストは判定が安定しないため、宣言値があればそれを使う。
MIN_DETECTION_LENGTH = 40

# 多くのサイトが未設定のまま残す値。宣言として信用しない。
UNRELIABLE_DECLARED_LANGUAGES = frozenset({"", "en-us", "en-gb", "x-default", "und"})


def normalize_language_tag(tag: str | None) -> str | None:
    """`ja-JP` や `EN_US` のような表記を主要部分だけに正規化する。"""
    if not tag:
        return None
    primary = tag.strip().lower().replace("_", "-").split("-")[0]
    return primary or None


def detect_language(text: str) -> str | None:
    """本文から言語を推定する。判定できなければ None。"""
    sample = text.strip()[:DETECTION_SAMPLE_LENGTH]
    if len(sample) < MIN_DETECTION_LENGTH:
        return None
    language, _score = py3langid.classify(sample)
    return normalize_language_tag(language)


def resolve_language(*, declared: str | None, body: str) -> str | None:
    """宣言値と本文推定から原文言語を決める。

    宣言値が信頼できる形（`ja` など具体的な指定）ならそれを使う。
    テンプレート初期値にありがちな値は信用せず、本文から推定する。
    どちらも得られなければ None を返し、判断を後段に委ねる。
    """
    normalized_declared = normalize_language_tag(declared)
    detected = detect_language(body)

    if declared and declared.strip().lower() in UNRELIABLE_DECLARED_LANGUAGES:
        return detected or normalized_declared

    return normalized_declared or detected
