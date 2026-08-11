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

# 走査が現実的な時間で終わることを確かめる回帰テストの上限（Issue #61）。
#
# CPU 時間の上限は、通す側と落とす側の実測の間に 1 桁以上のマージンを取って引く。
#
# - 通す側（現行の線形実装）: 0.18秒。カバレッジ計測で約3倍、pytest の並列実行に
#   よる CPU の奪い合いでさらに約3倍に伸びて、最悪 1.8秒までを実測した
# - 落とす側（かつての正規表現。`extract_first_url` の docstring 参照）:
#   `test_returns_none_in_linear_time_for_a_long_line_without_a_scheme` が与える
#   30万文字の入力に対して、同じマシンで 78.3秒（10万文字で 16.8秒、20万文字で
#   59.7秒と二次関数的に伸びる）
#
# 上限を 1.0秒に置いていた頃は、実装が線形のままでも並列実行で落ちた。壁時計を
# CPU 時間へ替えるだけでは足りず（キャッシュやメモリ帯域の奪い合いは CPU 時間にも
# 乗る）、入力を倍にしたときの伸び率で見る形も安定しなかった（入力サイズで
# キャッシュの効き方が変わり、線形のままでも 3.7倍を観測）。
_REDOS_CPU_SECONDS_LIMIT = 10.0

# 壁時計の上限。CPU 時間は待機（ロック待ち・I/O・sleep）を数えないため、CPU を
# 焼かずに詰まる形の劣化を取りこぼす。並列実行のぶれを吸収しつつ、その種のハングは
# 捕まえられる位置に、大きめの上限を併せて置く。
_REDOS_WALL_SECONDS_LIMIT = 60.0


def _assert_scans_without_blowing_up(line: str) -> None:
    """`extract_first_url` がこの行を現実的な時間で走査し切ることを確かめる。"""
    started_cpu = time.process_time()
    started_wall = time.perf_counter()
    url = extract_first_url(line)
    cpu_seconds = time.process_time() - started_cpu
    wall_seconds = time.perf_counter() - started_wall

    assert url is None
    assert cpu_seconds < _REDOS_CPU_SECONDS_LIMIT
    assert wall_seconds < _REDOS_WALL_SECONDS_LIMIT


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

    @pytest.mark.parametrize(
        "line",
        [
            "123://ignored real link https://example.com/a",
            "-://x https://example.com/a",
        ],
    )
    def test_keeps_looking_when_the_first_scheme_separator_is_not_part_of_a_url(
        self, line: str
    ) -> None:
        """行内の最初の "://" がスキームを伴わなくても、後ろにある本物のURLを見つける。

        最初の候補だけを見て諦めると、その行が「URLを含まない行」として黙って
        無視され、登録にもエラーにも数えられない。
        """
        # Act
        url = extract_first_url(line)

        # Assert
        assert url == "https://example.com/a"

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("http://[::1]:8080/health", "http://[::1]:8080/health"),
            (
                "https://example.com/api?ids[]=1&ids[]=2",
                "https://example.com/api?ids[]=1&ids[]=2",
            ),
            ("- [記事](https://example.com/api?ids[]=1)", "https://example.com/api?ids[]=1"),
            ("https://example.com/a]", "https://example.com/a"),
        ],
    )
    def test_keeps_balanced_square_brackets_in_the_url(self, line: str, expected: str) -> None:
        """角括弧も丸括弧と同じく対応を数える。

        IPv6 リテラルのホストやクエリの配列記法は角括弧を含む。対応を数えずに
        最初の `]` で打ち切ると URL が途中で切れるが、切れた URL もスキーム検証と
        長さ検証は通ってしまうため、エラー行にならないまま登録される。対応の
        取れていない `]` （Markdown の参照リンクなど）はこれまでどおり終端。
        """
        # Act
        url = extract_first_url(line)

        # Assert
        assert url == expected

    def test_returns_none_in_linear_time_for_a_long_line_of_invalid_separators(self) -> None:
        """受入基準（ReDoS回帰）: URL にならない "://" が大量に並ぶ行でも線形で終わる。

        「候補が URL にならなければ次の "://" から探し直す」ループが、候補ごとに
        行全体を舐め直す形になっていないことを固定する。

        固定できるのは線形走査自身の再走査バグまでで、正規表現への差し戻しは
        この入力では捕まえられない。旧正規表現はスキームの先頭を `[a-zA-Z]` に
        限るため、"1" で始まるこの入力は各開始位置の 1 文字目で不一致になり、
        バックトラッキングを起こさないまま 0.00秒で終わる（実測。Issue #61 の
        レビューで判明）。正規表現への差し戻しは
        `test_returns_none_in_linear_time_for_a_long_line_without_a_scheme`
        が捕まえる。
        """
        # Arrange
        line = ("1" * 20 + "://") * 50_000

        # Act / Assert
        _assert_scans_without_blowing_up(line)

    @pytest.mark.parametrize("line", ["http://", "http://....", "https://。"])
    def test_returns_none_when_the_scheme_has_no_host(self, line: str) -> None:
        """ホストの無い裸のスキームはURLとして扱わない。

        末尾の句読点を落とした結果 "http://" だけが残る行がある。これは
        `validate_bulk_import_url` のスキーム検証も長さ検証も通ってしまうため、
        抽出の時点で弾かないと壊れたURLがそのまま登録される。
        """
        # Act
        url = extract_first_url(line)

        # Assert
        assert url is None

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
        一瞬で None を返す。正規表現への差し戻しを実際に捕まえられるのはこちら。

        入力長は、旧正規表現との差が上限（`_REDOS_CPU_SECONDS_LIMIT`）を挟んで
        桁で開く位置に取る。同じマシンでの実測では、旧正規表現は 10万文字で
        16.8秒、20万文字で 59.7秒と二次関数的に伸び、ここで与える 30万文字では
        78.3秒かかった。現行の線形走査は 30万文字でも 0.0002秒で終わる
        （Issue #61）。
        """
        # Arrange
        long_line_without_scheme = "a" * 300_000

        # Act / Assert
        _assert_scans_without_blowing_up(long_line_without_scheme)


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
