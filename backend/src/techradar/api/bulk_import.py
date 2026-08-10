"""URL リストファイルの一括インポート用の純粋関数群（`PROJECT_SPEC.md` §6.2, Issue #39）。

DB セッションやジョブキューを扱う処理は `techradar.api.articles` 側に置き、ここには
副作用を持たない行パース・検証のみを集約する。単体テストしやすくするため
（`fetcher.url.normalize_url` と同じ方針）。
"""

from __future__ import annotations

import string
from dataclasses import dataclass

# アップロードファイルのサイズ上限（バイト）。全内容をメモリに載せる前に判定するため、
# `techradar.api.articles` の bulk エンドポイントは読み込みながらこの値を検査する。
MAX_BULK_IMPORT_FILE_BYTES = 1024 * 1024

# 抽出後の URL 件数の上限。見出し・空行を大量に含むファイルを誤って弾かないよう、
# 行数ではなく実際に抽出できた URL の件数で判定する。
MAX_BULK_IMPORT_URL_COUNT = 500

# エラー行の一覧に載せる元の行の最大長。極端に長い行をそのまま応答へ含めると
# 応答が肥大化するため切り詰める。
MAX_ERROR_LINE_PREVIEW_LENGTH = 200

# 受け付けるファイル拡張子（小文字判定）。
ALLOWED_BULK_IMPORT_EXTENSIONS = (".md", ".txt")

# URL のスキーム部分（`scheme://` の `scheme`）に使える文字。RFC 3986 では
# スキームは ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) と定義されており、
# 先頭は英字に限られる。
_SCHEME_CHARS = frozenset(string.ascii_letters + string.digits + "+.-")

# URL 本文を打ち切る記号。空白・引用符・山括弧が現れたら即座に URL の終端と
# みなす。括弧は対応を数えて別扱いする（`_BRACKET_PAIRS` と `_find_url_end`）。
_URL_STOP_CHARS = frozenset("\"'<>")

# URL 本文に現れる括弧。対応が取れている分は URL の一部として残し、対応の無い
# 閉じ括弧が来たらそこを終端とする。Markdown リンク `[title](url)` を閉じる
# 括弧を巻き込まない一方で、URL 自身が含む括弧は保つ。
#
# 丸括弧: `https://en.wikipedia.org/wiki/Foo_(disambiguation)`
# 角括弧: IPv6 リテラル `http://[::1]:8080/`、クエリの配列記法 `?ids[]=1`
#
# 対応を数えずに閉じ括弧を一律の終端にすると、これらが途中で切れる。切れた
# URL もスキーム検証と長さ検証は通ってしまうため、エラー行として報告されない
# まま登録され、取得ジョブだけが失敗する（利用者からは成功に見える）。
_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSING_TO_OPENING_BRACKET = {closing: opening for opening, closing in _BRACKET_PAIRS.items()}

# URL 末尾に巻き込まれがちな文末の句読点。`_strip_trailing_punctuation` で
# 取り除く対象。丸括弧の対応が取れた末尾の `)`（例: Wikipedia の
# `Foo_(disambiguation)`）はここには含めない。対応の取れた `)` は
# `_find_url_end` の時点で URL 本文に残すべきものとして処理済みのため。
_TRAILING_PUNCTUATION_CHARS = frozenset(".,;:!?。、！？；：")


@dataclass(frozen=True)
class ParsedUrlLine:
    """ファイルの1行から抽出できた URL とその出現位置。"""

    line_number: int
    original_line: str
    url: str


def _find_scheme_start(line: str, scheme_end: int) -> int | None:
    """ "://" の直前の位置（`scheme_end`）から後方へスキーム文字を辿り、
    URL の開始位置を探す。

    スキームの先頭は英字でなければならないため、後方に連続するスキーム文字の
    範囲のうち最も左（最も早く現れる）英字の位置を返す。見つからなければ None。
    """
    low = scheme_end
    while low > 0 and line[low - 1] in _SCHEME_CHARS:
        low -= 1
    for index in range(low, scheme_end):
        if line[index] in string.ascii_letters:
            return index
    return None


def _find_url_end(line: str, body_start: int) -> int:
    """URL 本文の終端位置を、括弧の対応を数えながら前向きに走査して決める。

    `_URL_STOP_CHARS` に含まれる文字・空白が現れたら即座に終端とする。括弧は
    種類ごとに深さを数え、対応の取れていない閉じ括弧が現れた時点で終端とする
    （Markdown の `[title](url)` を閉じる括弧を URL に巻き込まないため）。
    対応の取れた括弧（例: `Foo_(disambiguation)`、`http://[::1]:8080/`）は
    そのまま URL 本文に含める。
    """
    depths = dict.fromkeys(_BRACKET_PAIRS, 0)
    index = body_start
    length = len(line)
    while index < length:
        char = line[index]
        if char.isspace() or char in _URL_STOP_CHARS:
            break
        if char in _BRACKET_PAIRS:
            depths[char] += 1
        elif (opening := _CLOSING_TO_OPENING_BRACKET.get(char)) is not None:
            if depths[opening] == 0:
                break
            depths[opening] -= 1
        index += 1
    return index


def _strip_trailing_punctuation(url: str) -> str:
    """URL 末尾に巻き込まれた文末の句読点を取り除く。"""
    end = len(url)
    while end > 0 and url[end - 1] in _TRAILING_PUNCTUATION_CHARS:
        end -= 1
    return url[:end]


def extract_first_url(line: str) -> str | None:
    """行から最初に現れる URL（`scheme://...`）を取り出す。見つからなければ None。

    スキームが http/https かどうかはここでは判定しない（`validate_bulk_import_url`
    の責務）。ここで http/https 以外を弾いてしまうと、不正スキームの行を
    「URL を含まない行」として無視してしまい、エラー行として報告できなくなる。

    正規表現ではなく手書きの線形走査で実装している理由: ReDoS。かつての実装
    `[a-zA-Z][a-zA-Z0-9+.-]*://[^\\s)\\]}\"'<>]+` は "://" を含まない長い行に
    対してバックトラッキングが二次関数的に増大し、実測で行長 4万文字で約1.4秒
    かかった（10000: 0.1秒、20000: 0.35秒、40000: 1.43秒という増え方から、
    100万文字では外挿で約16分ハングする）。改行を含まない1行はファイルサイズ
    上限（`MAX_BULK_IMPORT_FILE_BYTES`）でしか弾かれないため、拡張子・サイズ・
    UTF-8 検証をすべて通過してこの関数に到達し得る。バックエンドはジョブ
    ワーカーと同一プロセスに同居する構成のため、ここが詰まると巡回処理まで
    止まる。本実装はバックトラッキングを行わず、各文字を定数回走査するだけの
    O(n) で完了する。「正規表現の方が短い」という理由で正規表現に戻さないこと
    （Issue #39 self review で検出・修正）。
    """
    search_from = 0
    while True:
        scheme_end = line.find("://", search_from)
        if scheme_end == -1:
            return None
        url = _extract_url_at(line, scheme_end)
        if url is not None:
            return url
        # この "://" は URL の一部ではなかった（スキームが無い、本文が無い等）。
        # 同じ行の後ろに本物の URL があり得るため、次の "://" から探し直す。
        search_from = scheme_end + 3


def _extract_url_at(line: str, scheme_end: int) -> str | None:
    """`line` の `scheme_end` にある "://" を起点に URL を切り出す。

    その位置が URL を成していなければ None を返す（呼び出し側が次の "://" を
    探し直す）。
    """
    start = _find_scheme_start(line, scheme_end)
    if start is None:
        return None

    body_start = scheme_end + 3
    end = _find_url_end(line, body_start)
    url = _strip_trailing_punctuation(line[start:end])
    if len(url) <= body_start - start:
        # スキームの直後に本文が残らない（"http://" だけの行、あるいは
        # "http://...." のように本文が句読点だけで消えた行）。ホストの無い
        # 裸のスキームはスキーム検証も長さ検証も通ってしまうため、ここで
        # URL として扱わない。
        return None
    return url


def parse_url_lines(text: str) -> list[ParsedUrlLine]:
    """テキストを行ごとに走査し、URL らしき文字列を含む行だけを出現順に抽出する。

    見出し・空行・URL を含まない行は無視する（エラーとしては数えない）。
    1行に複数 URL があっても最初の1つだけを採用する。
    """
    parsed: list[ParsedUrlLine] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        url = extract_first_url(raw_line)
        if url is None:
            continue
        parsed.append(ParsedUrlLine(line_number=line_number, original_line=raw_line, url=url))
    return parsed


def validate_bulk_import_url(
    url: str, *, allowed_schemes: tuple[str, ...], max_length: int
) -> str | None:
    """抽出した URL がそのまま登録可能かを検証する。

    不正であれば理由文字列を、問題なければ None を返す。許容スキーム・最大長は
    呼び出し側（`techradar.api.articles` の `_ALLOWED_URL_SCHEMES` /
    `MAX_URL_LENGTH`）が持つ値をそのまま渡してもらい、ここでは重複定義しない。
    """
    if not url.startswith(allowed_schemes):
        return "http または https で始まる URL のみ登録できます"
    if len(url) > max_length:
        return f"URLが長すぎます（{max_length}文字以内にしてください）"
    return None


def truncate_line_preview(line: str) -> str:
    """エラー行に載せる元の行を、応答肥大化を防ぐため切り詰める。"""
    return line[:MAX_ERROR_LINE_PREVIEW_LENGTH]


def has_allowed_bulk_import_extension(filename: str | None) -> bool:
    """アップロードされたファイル名が受付対象の拡張子（.md / .txt）かを判定する。"""
    if filename is None:
        return False
    return filename.lower().endswith(ALLOWED_BULK_IMPORT_EXTENSIONS)
