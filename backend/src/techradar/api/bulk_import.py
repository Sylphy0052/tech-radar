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

# URL 本文を打ち切る記号。空白・引用符・山括弧・閉じ大括弧/中括弧が現れたら
# 即座に URL の終端とみなす（Markdown リンク `[title](url)` の前後の装飾文字を
# URL に巻き込まないため）。丸括弧 `(` `)` は対応を数えて別扱いする
# （`_find_url_end` を参照）。
_URL_STOP_CHARS = frozenset("\"'<>]}")

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
    """URL 本文の終端位置を、丸括弧の対応を数えながら前向きに走査して決める。

    `_URL_STOP_CHARS` に含まれる文字・空白が現れたら即座に終端とする。丸括弧は
    深さを数え、対応の取れていない `)` が現れた時点で終端とする（Markdown の
    `[title](url)` を閉じる括弧を URL に巻き込まないため）。対応の取れた `)`
    （例: `Foo_(disambiguation)`）はそのまま URL 本文に含める。
    """
    depth = 0
    index = body_start
    length = len(line)
    while index < length:
        char = line[index]
        if char.isspace() or char in _URL_STOP_CHARS:
            break
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
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
    scheme_end = line.find("://")
    if scheme_end == -1:
        return None

    start = _find_scheme_start(line, scheme_end)
    if start is None:
        return None

    body_start = scheme_end + 3
    end = _find_url_end(line, body_start)
    if end == body_start:
        # スキームの直後に本文が無い（例: "http://" だけの行）。
        return None

    return _strip_trailing_punctuation(line[start:end])


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
