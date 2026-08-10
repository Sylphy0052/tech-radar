"""URL リストファイルの一括インポート用パーサを検証する（Issue #39）。"""

from __future__ import annotations

import time

import pytest

from techradar.api.bulk_import import (
    MAX_ERROR_LINE_PREVIEW_LENGTH,
    ParsedUrlLine,
    extract_first_url,
    has_allowed_bulk_import_extension,
    parse_url_lines,
    truncate_line_preview,
    validate_bulk_import_url,
)

_ALLOWED_SCHEMES = ("http://", "https://")
_MAX_URL_LENGTH = 2048


class TestExtractFirstUrl:
    def test_extracts_the_url_from_a_markdown_link_line(self) -> None:
        """受入基準: Markdownリンク行から閉じ括弧を含めずURLが抽出される。"""
        # Act
        url = extract_first_url("- [タイトル](https://example.com/a)")

        # Assert
        assert url == "https://example.com/a"

    def test_extracts_the_url_from_a_bare_url_line(self) -> None:
        """受入基準: 素URL単体の行からそのまま抽出される。"""
        # Act
        url = extract_first_url("https://example.com/a")

        # Assert
        assert url == "https://example.com/a"

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("## 7月下旬", id="heading-line"),
            pytest.param("", id="empty-line"),
            pytest.param("これはメモです。URLはありません。", id="no-url-line"),
        ],
    )
    def test_returns_none_for_lines_without_a_url(self, line: str) -> None:
        """受入基準: 見出し・空行・URLを含まない行はNoneになる（エラーには数えない）。"""
        # Act
        url = extract_first_url(line)

        # Assert
        assert url is None

    def test_extracts_only_the_first_url_when_a_line_has_multiple(self) -> None:
        """受入基準: 1行に複数URLがあっても最初の1つだけ抽出される。"""
        # Act
        url = extract_first_url("https://example.com/a https://example.com/b")

        # Assert
        assert url == "https://example.com/a"

    def test_extracts_a_url_with_a_disallowed_scheme_so_it_can_be_reported_as_an_error(
        self,
    ) -> None:
        """不正スキームの行を「URLを含まない行」として無視してしまうと、
        `validate_bulk_import_url` によるエラー報告に到達できなくなる。
        スキームの許可判定は抽出ではなく検証（`validate_bulk_import_url`）の
        責務のため、抽出自体は http/https 以外のスキームも取り出す。
        """
        # Act
        url = extract_first_url("ftp://example.com/a")

        # Assert
        assert url == "ftp://example.com/a"

    def test_keeps_the_balanced_closing_paren_inside_a_wikipedia_style_url(self) -> None:
        """受入基準: 括弧を含むURL単体（Wikipedia風）は、対応の取れた閉じ括弧を
        巻き込んだまま抽出される。
        """
        # Act
        url = extract_first_url("https://en.wikipedia.org/wiki/Foo_(disambiguation)")

        # Assert
        assert url == "https://en.wikipedia.org/wiki/Foo_(disambiguation)"

    def test_keeps_the_balanced_paren_but_drops_the_markdown_link_closing_paren(self) -> None:
        """受入基準: Markdownリンクの中に括弧入りURLがあっても、URL側の対応が
        取れた閉じ括弧は残しつつ、リンクを閉じる括弧は巻き込まない。
        """
        # Act
        url = extract_first_url("- [記事](https://en.wikipedia.org/wiki/Foo_(disambiguation))")

        # Assert
        assert url == "https://en.wikipedia.org/wiki/Foo_(disambiguation)"

    def test_trims_a_trailing_full_width_period_after_a_bare_url_in_a_sentence(self) -> None:
        """受入基準: 文中の素URLの直後にある半角ピリオドは巻き込まない。"""
        # Act
        url = extract_first_url("See https://example.com/a.")

        # Assert
        assert url == "https://example.com/a"

    def test_trims_a_trailing_full_width_punctuation_after_a_bare_url_in_japanese_text(
        self,
    ) -> None:
        """受入基準: 日本語文中の素URLの直後にある全角句点は巻き込まない。"""
        # Act
        url = extract_first_url("詳しくは https://example.com/a を参照。")

        # Assert
        assert url == "https://example.com/a"

    def test_trims_a_trailing_comma_inside_parentheses(self) -> None:
        """受入基準: 括弧内でURLの後にカンマが続く場合、カンマは巻き込まない。"""
        # Act
        url = extract_first_url("(https://example.com/a, ok)")

        # Assert
        assert url == "https://example.com/a"

    def test_returns_none_in_linear_time_for_a_long_line_without_a_scheme(self) -> None:
        """受入基準（ReDoS回帰）: "://" を含まない長い行は、バックトラッキング
        のある正規表現なら二次関数的に劣化する入力だが、線形走査であれば
        一瞬で None を返す。
        """
        # Arrange
        long_line_without_scheme = "a" * 100_000

        # Act
        started_at = time.perf_counter()
        url = extract_first_url(long_line_without_scheme)
        elapsed_seconds = time.perf_counter() - started_at

        # Assert
        assert url is None
        assert elapsed_seconds < 1.0


class TestParseUrlLines:
    def test_extracts_urls_in_order_and_skips_non_url_lines(self) -> None:
        """受入基準: 見出し・空行・URL非含有行は無視され、URLを含む行だけが出現順に残る。"""
        # Arrange
        text = "## 7月下旬\n\n- [記事A](https://example.com/a)\nメモ行\nhttps://example.com/b\n"

        # Act
        parsed = parse_url_lines(text)

        # Assert
        assert parsed == [
            ParsedUrlLine(
                line_number=3,
                original_line="- [記事A](https://example.com/a)",
                url="https://example.com/a",
            ),
            ParsedUrlLine(
                line_number=5, original_line="https://example.com/b", url="https://example.com/b"
            ),
        ]

    def test_returns_an_empty_list_for_text_without_any_url(self) -> None:
        # Act
        parsed = parse_url_lines("## 見出しのみ\n\nメモだけ\n")

        # Assert
        assert parsed == []


class TestValidateBulkImportUrl:
    def test_returns_none_for_a_valid_url(self) -> None:
        # Act
        reason = validate_bulk_import_url(
            "https://example.com/a", allowed_schemes=_ALLOWED_SCHEMES, max_length=_MAX_URL_LENGTH
        )

        # Assert
        assert reason is None

    def test_returns_a_reason_for_a_disallowed_scheme(self) -> None:
        # Act
        reason = validate_bulk_import_url(
            "ftp://example.com/a", allowed_schemes=_ALLOWED_SCHEMES, max_length=_MAX_URL_LENGTH
        )

        # Assert
        assert reason is not None

    def test_returns_a_reason_for_a_url_longer_than_the_limit(self) -> None:
        # Arrange
        too_long_url = "https://example.com/" + "a" * _MAX_URL_LENGTH

        # Act
        reason = validate_bulk_import_url(
            too_long_url, allowed_schemes=_ALLOWED_SCHEMES, max_length=_MAX_URL_LENGTH
        )

        # Assert
        assert reason is not None

    def test_returns_none_for_a_url_exactly_at_the_max_length(self) -> None:
        """境界値: max_length ちょうどの長さのURLは許可される。"""
        # Arrange
        prefix = "https://example.com/"
        exactly_max_length_url = prefix + "a" * (_MAX_URL_LENGTH - len(prefix))

        # Act
        reason = validate_bulk_import_url(
            exactly_max_length_url,
            allowed_schemes=_ALLOWED_SCHEMES,
            max_length=_MAX_URL_LENGTH,
        )

        # Assert
        assert len(exactly_max_length_url) == _MAX_URL_LENGTH
        assert reason is None

    def test_returns_a_reason_for_a_url_one_char_over_the_max_length(self) -> None:
        """境界値: max_length + 1文字のURLはエラーになる。"""
        # Arrange
        prefix = "https://example.com/"
        one_over_max_length_url = prefix + "a" * (_MAX_URL_LENGTH - len(prefix) + 1)

        # Act
        reason = validate_bulk_import_url(
            one_over_max_length_url,
            allowed_schemes=_ALLOWED_SCHEMES,
            max_length=_MAX_URL_LENGTH,
        )

        # Assert
        assert len(one_over_max_length_url) == _MAX_URL_LENGTH + 1
        assert reason is not None


class TestTruncateLinePreview:
    def test_keeps_a_short_line_unchanged(self) -> None:
        # Act
        preview = truncate_line_preview("https://example.com/a")

        # Assert
        assert preview == "https://example.com/a"

    def test_truncates_a_line_longer_than_the_limit(self) -> None:
        # Arrange
        long_line = "a" * (MAX_ERROR_LINE_PREVIEW_LENGTH + 100)

        # Act
        preview = truncate_line_preview(long_line)

        # Assert
        assert len(preview) == MAX_ERROR_LINE_PREVIEW_LENGTH


class TestHasAllowedBulkImportExtension:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            pytest.param("urls.md", True, id="md-extension"),
            pytest.param("urls.txt", True, id="txt-extension"),
            pytest.param("URLS.MD", True, id="uppercase-extension"),
            pytest.param("urls.csv", False, id="unsupported-extension"),
            pytest.param(None, False, id="missing-filename"),
        ],
    )
    def test_judges_the_extension(self, filename: str | None, expected: bool) -> None:
        # Act
        result = has_allowed_bulk_import_extension(filename)

        # Assert
        assert result is expected
