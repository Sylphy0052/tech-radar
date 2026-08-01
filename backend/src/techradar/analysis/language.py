"""原文言語の判定（`PROJECT_SPEC.md` §16）。

LLM を使わず軽量ライブラリで判定する。言語判定のためだけに LLM を呼ぶのは
コストに見合わない（`PROJECT_SPEC.md` §24 コスト管理）。

**本文からの推定を優先する。** `<html lang>` はテンプレートの初期値が
そのまま残っているサイトが多く、宣言を信じると英語記事を日本語と誤認して
日本語タイトルが作られない、といった実害が出る。
推定できなかった場合にのみ宣言値を使う。
"""

from __future__ import annotations

import py3langid

# 判定に使う本文の長さ。全文を渡しても精度は上がらず時間だけ延びる。
DETECTION_SAMPLE_LENGTH = 2000

# これ未満のテキストは判定が安定しないため、宣言値があればそれを使う。
MIN_DETECTION_LENGTH = 40

# 言語コードとして採用しない値。`x-default` の先頭要素は `x` になってしまい、
# そのまま保存すると意味のないコードが残る。
INVALID_LANGUAGE_CODES = frozenset({"x", "und", "zxx", "mul", "qaa"})


def normalize_language_tag(tag: str | None) -> str | None:
    """`ja-JP` や `EN_US` のような表記を主要部分だけに正規化する。

    言語コードとして意味を成さない値は None にする。
    """
    if not tag:
        return None
    primary = tag.strip().lower().replace("_", "-").split("-")[0]
    if not primary or primary in INVALID_LANGUAGE_CODES:
        return None
    # ISO 639-1 / 639-2 は 2〜3 文字。それ以外は表記ゆれとみなし採用しない。
    if not primary.isalpha() or not (2 <= len(primary) <= 3):
        return None
    return primary


def detect_language(text: str) -> str | None:
    """本文から言語を推定する。判定できなければ None。"""
    sample = text.strip()[:DETECTION_SAMPLE_LENGTH]
    if len(sample) < MIN_DETECTION_LENGTH:
        return None
    language, _score = py3langid.classify(sample)
    return normalize_language_tag(language)


def resolve_language(*, declared: str | None, body: str) -> str | None:
    """宣言値と本文推定から原文言語を決める。

    **本文から推定できたならそれを採る。** 宣言値は推定できなかったときの
    フォールバックとしてのみ使う。

    宣言を優先すると、`<html lang="en">` のままの日本語記事や、逆に
    `lang="ja"` のままの英語記事を取り違える。言語は `translated_title` を
    作るかどうかの判断に直結するため、実際の本文を根拠にする。
    """
    detected = detect_language(body)
    if detected is not None:
        return detected
    return normalize_language_tag(declared)
