r"""`db.query.escape_like_pattern` の単体テスト（Issue #94）。

`GET /api/feed`・`GET /api/articles`・`GET /api/sources` の検索語はいずれも
LIKE の特殊文字（`%` / `_`）をエスケープしてから `ilike` のパターンへ埋め込む
必要がある。この関数はその共通処理そのものなので、エンドポイント経由の
間接テストとは別に、置換順序（`\` を先、`%` / `_` を後）が守られていることを
直接確かめる。
"""

from __future__ import annotations

from techradar.db.query import escape_like_pattern


class TestEscapeLikePattern:
    def test_escapes_percent(self) -> None:
        # Arrange & Act & Assert — 受入基準: `%` を含む検索語はワイルドカードとして解釈されない
        assert escape_like_pattern("100%") == "100\\%"

    def test_escapes_underscore(self) -> None:
        # Arrange & Act & Assert — 受入基準: `_` を含む検索語は任意の1文字として解釈されない
        assert escape_like_pattern("foo_bar") == "foo\\_bar"

    def test_escapes_backslash(self) -> None:
        # Arrange & Act & Assert
        assert escape_like_pattern("a\\b") == "a\\\\b"

    def test_leaves_a_plain_value_unchanged(self) -> None:
        # Arrange & Act & Assert
        assert escape_like_pattern("Python入門") == "Python入門"

    def test_does_not_double_escape_when_backslash_and_percent_are_both_present(self) -> None:
        # Arrange — `\` を先に処理しないと、`%` を `\%` にした直後の `\` が
        # 再びエスケープされて `\\%` になり、リテラルの `\` + ワイルドカードに
        # 化けてしまう。この入力はその順序ミスを踏むとすぐ落ちる
        value = "50%\\off"

        # Act
        result = escape_like_pattern(value)

        # Assert — `\` → `\\`、`%` → `\%` の順で、二重エスケープになっていないこと
        assert result == "50\\%\\\\off"
