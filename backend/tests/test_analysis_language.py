"""原文言語の判定を検証する（`PROJECT_SPEC.md` §16）。"""

from __future__ import annotations

import pytest

from techradar.analysis.language import (
    detect_language,
    normalize_language_tag,
    resolve_language,
)

JAPANESE_BODY = (
    "Model Context Protocol は、大規模言語モデルを外部のツールへ接続するための"
    "標準的な仕組みです。本稿では実装手順を具体的なコードとともに解説します。"
)
ENGLISH_BODY = (
    "Model Context Protocol is an open standard that connects large language models "
    "to external tools. This article walks through the implementation step by step."
)


class TestNormalizeLanguageTag:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ja", "ja"),
            ("ja-JP", "ja"),
            ("EN_US", "en"),
            ("  zh-Hans-CN  ", "zh"),
            ("", None),
            (None, None),
        ],
    )
    def test_normalizes(self, raw: str | None, expected: str | None):
        # Arrange / Act / Assert
        assert normalize_language_tag(raw) == expected


class TestDetectLanguage:
    def test_detects_japanese(self):
        # Arrange / Act / Assert
        assert detect_language(JAPANESE_BODY) == "ja"

    def test_detects_english(self):
        # Arrange / Act / Assert
        assert detect_language(ENGLISH_BODY) == "en"

    def test_returns_none_for_short_text(self):
        # Arrange / Act / Assert — 短すぎる入力は判定が安定しない
        assert detect_language("hi") is None

    def test_returns_none_for_blank_text(self):
        # Arrange / Act / Assert
        assert detect_language("   \n  ") is None


class TestResolveLanguage:
    def test_uses_a_specific_declared_language(self):
        # Arrange / Act — 具体的な宣言があればそれを尊重する
        resolved = resolve_language(declared="ja-JP", body=ENGLISH_BODY)

        # Assert
        assert resolved == "ja"

    @pytest.mark.parametrize("declared", ["", "en-US", "x-default", "und"])
    def test_ignores_template_defaults_and_detects_from_body(self, declared: str):
        # Arrange / Act — テンプレート初期値のまま放置されたサイトが多い
        resolved = resolve_language(declared=declared, body=JAPANESE_BODY)

        # Assert
        assert resolved == "ja"

    def test_detects_from_body_when_nothing_is_declared(self):
        # Arrange / Act
        resolved = resolve_language(declared=None, body=ENGLISH_BODY)

        # Assert
        assert resolved == "en"

    def test_returns_none_when_neither_is_available(self):
        # Arrange / Act / Assert — 判断は後段へ委ねる
        assert resolve_language(declared=None, body="") is None

    def test_falls_back_to_declared_when_body_is_too_short(self):
        # Arrange / Act — 本文が短くても宣言があれば使う
        resolved = resolve_language(declared="en-US", body="ok")

        # Assert
        assert resolved == "en"
