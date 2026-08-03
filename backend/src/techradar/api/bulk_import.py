"""URL リストファイルの一括インポート用の純粋関数群（`PROJECT_SPEC.md` §6.2, Issue #39）。

DB セッションやジョブキューを扱う処理は `techradar.api.articles` 側に置き、ここには
副作用を持たない行パース・検証のみを集約する。単体テストしやすくするため
（`fetcher.url.normalize_url` と同じ方針）。
"""

from __future__ import annotations

import re
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

# 行内から最初に現れる URL（`scheme://...` 形式）を取り出す正規表現。
# 空白・引用符・山括弧・閉じ括弧で URL を打ち切ることで、Markdown リンク
# `[title](url)` の閉じ括弧や前後の装飾文字を URL に巻き込まないようにする。
#
# 抽出対象は http(s) に限定しない。想定される入力の大半は http(s) の URL だが、
# ここで ftp: 等の他スキームも "URL を含まない行" として無視してしまうと、
# `validate_bulk_import_url` によるスキーム検証（不正スキームを行番号・理由付きの
# エラー行として応答へ含める）に到達できず、実質的に握り潰されてしまう。
# 「URL を含まない行は無視する」対象は、そもそも scheme://... の形をしていない
# 行（見出し・空行・本文など）に限定する。
_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s)\]}\"'<>]+")


@dataclass(frozen=True)
class ParsedUrlLine:
    """ファイルの1行から抽出できた URL とその出現位置。"""

    line_number: int
    original_line: str
    url: str


def extract_first_url(line: str) -> str | None:
    """行から最初に現れる URL（`scheme://...`）を取り出す。見つからなければ None。

    スキームが http/https かどうかはここでは判定しない（`validate_bulk_import_url`
    の責務）。ここで http/https 以外を弾いてしまうと、不正スキームの行を
    「URL を含まない行」として無視してしまい、エラー行として報告できなくなる。
    """
    match = _URL_PATTERN.search(line)
    return match.group(0) if match else None


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
