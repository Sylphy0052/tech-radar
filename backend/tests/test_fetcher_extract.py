"""本文抽出とサニタイズを検証する。"""

from __future__ import annotations

from datetime import UTC

import pytest

from techradar.fetcher.errors import ExtractionError
from techradar.fetcher.extract import (
    compute_body_hash,
    extract_article,
    sanitize_html,
    strip_site_suffix,
)

BODY_TEXT = (
    "Model Context Protocol は、LLM が外部ツールへ接続するための標準的な仕組みです。"
    "本稿では MCP サーバーの実装手順を、実際のコードを交えて詳しく解説します。"
    "まず依存関係を導入し、次にツール定義を記述し、最後に動作を確認します。"
    "この一連の流れを理解すると、任意のツールを安全に接続できるようになります。"
)

# trafilatura は同一段落を重複として除去するため、2 段落目は別内容にする。
SECOND_PARAGRAPH = (
    "続いて、ツールの入力スキーマを JSON Schema で定義します。"
    "型が厳密であるほどモデルの呼び出し精度は安定し、想定外の引数を早期に弾けます。"
    "最後に、エラー時の応答をどう返すべきかを整理して締めくくります。"
)


def article_html(*, extra_head: str = "", extra_body: str = "", lang: str = "ja") -> str:
    """テスト用の記事 HTML を組み立てる。"""
    return f"""<!doctype html>
<html lang="{lang}">
  <head>
    <title>MCP サーバー実装ガイド</title>
    <meta property="article:published_time" content="2026-07-28T09:30:00Z" />
    <meta name="author" content="Example Author" />
    {extra_head}
  </head>
  <body>
    <article>
      <h1>MCP サーバー実装ガイド</h1>
      <p>{BODY_TEXT}</p>
      <p>{SECOND_PARAGRAPH}</p>
    </article>
    {extra_body}
  </body>
</html>"""


class TestSanitizeHtml:
    @pytest.mark.parametrize(
        "dangerous",
        [
            "<script>alert(1)</script>",
            "<iframe src='https://evil.example.net'></iframe>",
            "<object data='x.swf'></object>",
            "<embed src='x.swf'>",
            "<noscript>fallback</noscript>",
            "<form action='https://evil.example.net'><input name='x'></form>",
        ],
    )
    def test_removes_dangerous_elements(self, dangerous: str):
        # Arrange / Act
        sanitized = sanitize_html(f"<html><body>{dangerous}<p>safe</p></body></html>")

        # Assert
        assert "evil.example.net" not in sanitized
        assert "alert(1)" not in sanitized
        assert "safe" in sanitized

    def test_removes_inline_event_handlers(self):
        # Arrange / Act
        sanitized = sanitize_html("<html><body><div onclick='steal()'>x</div></body></html>")

        # Assert
        assert "onclick" not in sanitized
        assert "steal()" not in sanitized

    def test_removes_javascript_urls(self):
        # Arrange / Act
        sanitized = sanitize_html("<html><body><a href='javascript:alert(1)'>x</a></body></html>")

        # Assert
        assert "javascript:" not in sanitized


class TestExtractArticle:
    def test_extracts_title_body_date_and_language(self):
        # Arrange
        html = article_html()

        # Act
        extracted = extract_article(html, "https://example.com/posts/1")

        # Assert
        assert extracted.title == "MCP サーバー実装ガイド"
        assert "Model Context Protocol" in extracted.body
        assert extracted.published_at is not None
        assert extracted.published_at.tzinfo is not None
        assert extracted.published_at.astimezone(UTC).year == 2026
        assert extracted.language == "ja"
        assert extracted.body_hash

    def test_extracts_language_from_html_tag_for_foreign_articles(self):
        # Arrange — 言語を限定しない要件のため原文言語を保持する
        html = article_html(lang="en")

        # Act
        extracted = extract_article(html, "https://example.com/posts/1")

        # Assert
        assert extracted.language == "en"

    def test_uses_canonical_link_when_present(self):
        # Arrange — フォールバック結果と別の URL を canonical に指定し、
        # 実際に canonical が使われていることを確かめる
        html = article_html(
            extra_head='<link rel="canonical" href="https://example.com/posts/1" />'
        )

        # Act
        extracted = extract_article(html, "https://example.com/posts/1-old?utm_source=x")

        # Assert
        assert extracted.canonical_url == "https://example.com/posts/1"

    def test_falls_back_to_normalized_source_url_without_canonical(self):
        # Arrange / Act
        extracted = extract_article(
            article_html(), "https://example.com/posts/1/?utm_source=x&b=2&a=1"
        )

        # Assert
        assert extracted.canonical_url == "https://example.com/posts/1?a=1&b=2"

    def test_does_not_include_script_contents_in_body(self):
        # Arrange — サニタイズ前に抽出するとスクリプト中の文字列が混入する
        html = article_html(
            extra_body="<script>var leak = 'SECRET_TOKEN_SHOULD_NOT_APPEAR';</script>"
        )

        # Act
        extracted = extract_article(html, "https://example.com/posts/1")

        # Assert
        assert "SECRET_TOKEN_SHOULD_NOT_APPEAR" not in extracted.body

    def test_rejects_pages_without_meaningful_body(self):
        # Arrange
        html = "<html lang='ja'><head><title>空</title></head><body><p>短い</p></body></html>"

        # Act / Assert
        with pytest.raises(ExtractionError):
            extract_article(html, "https://example.com/empty")

    def test_returns_none_for_unparsable_published_date(self):
        # Arrange
        html = article_html().replace("2026-07-28T09:30:00Z", "いつか")

        # Act
        extracted = extract_article(html, "https://example.com/posts/1")

        # Assert — 解釈できない日付は None にして後段の判断へ委ねる
        assert extracted.published_at is None


class TestStripSiteSuffix:
    @pytest.mark.parametrize(
        ("title", "site_name", "expected"),
        [
            pytest.param(
                "Go 1.24 is released! - The Go Programming Language",
                "The Go Programming Language",
                "Go 1.24 is released!",
                id="hyphen",
            ),
            pytest.param("MCPとは何か | Example Blog", "Example Blog", "MCPとは何か", id="pipe"),
            pytest.param(
                "記事タイトル｜テックブログ",
                "テックブログ",
                "記事タイトル",
                id="fullwidth-pipe",
            ),
            pytest.param(
                "Title - Other Site",
                "My Site",
                "Title - Other Site",
                id="keeps-unknown-suffix",
            ),
            pytest.param("Plain Title", "My Site", "Plain Title", id="no-separator"),
            pytest.param("Title", None, "Title", id="unknown-site-name"),
            pytest.param("- My Site", "My Site", "- My Site", id="keeps-when-head-would-be-empty"),
        ],
    )
    def test_strips_only_matching_site_name(self, title: str, site_name: str | None, expected: str):
        # Arrange / Act / Assert
        assert strip_site_suffix(title, [site_name]) == expected


class TestTitleSelection:
    def test_prefers_og_title_over_document_title(self):
        # Arrange — og:title が最も記事名に近い
        html = article_html(
            extra_head=(
                '<meta property="og:title" content="MCP サーバー実装ガイド" />'
                '<meta property="og:site_name" content="Example Blog" />'
            )
        ).replace(
            "<title>MCP サーバー実装ガイド</title>",
            "<title>MCP サーバー実装ガイド | Example Blog</title>",
        )

        # Act
        extracted = extract_article(html, "https://example.com/posts/1")

        # Assert
        assert extracted.title == "MCP サーバー実装ガイド"

    def test_strips_site_name_from_document_title(self):
        # Arrange — og:title が無く <title> にサイト名が付く一般的なケース
        html = article_html(
            extra_head='<meta property="og:site_name" content="Example Blog" />'
        ).replace(
            "<title>MCP サーバー実装ガイド</title>",
            "<title>MCP サーバー実装ガイド - Example Blog</title>",
        )

        # Act
        extracted = extract_article(html, "https://example.com/posts/1")

        # Assert — サイト名だけが残る不具合を防ぐ
        assert extracted.title == "MCP サーバー実装ガイド"


class TestComputeBodyHash:
    def test_is_stable_for_the_same_text(self):
        # Arrange / Act / Assert
        assert compute_body_hash("abc def") == compute_body_hash("abc def")

    def test_ignores_whitespace_differences(self):
        # Arrange / Act / Assert — 改行や空白の揺れで別記事と判定しない
        assert compute_body_hash("abc  def\n") == compute_body_hash("abc def")

    def test_differs_for_different_text(self):
        # Arrange / Act / Assert
        assert compute_body_hash("abc") != compute_body_hash("abd")
