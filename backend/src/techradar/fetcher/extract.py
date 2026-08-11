"""記事本文の抽出とサニタイズ（`PROJECT_SPEC.md` §21）。

JavaScript は実行しない。抽出前に `script` / `iframe` / `object` などの
危険な要素を除去し、本文はプレーンテキストとして取り出す。

抽出結果は非信頼入力のまま扱う。LLM へ渡す際の防御は Issue #4 が担う。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import trafilatura
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDocument

from techradar.fetcher.errors import ExtractionError
from techradar.fetcher.url import resolve_canonical_url

# スクリプト実行や外部読み込みにつながる要素は抽出前に落とす。
DANGEROUS_TAGS = (
    "script",
    "iframe",
    "object",
    "embed",
    "applet",
    "noscript",
    "form",
    "svg",
    "link",
    "style",
    # 相対 URL の解決先を差し替えられるため除去する。
    "base",
    "meta[http-equiv]",
)

# 空要素として書かれる想定のタグ。パーサが後続要素を子として取り込むことがあるため、
# 除去前に子を親へ引き上げないと正当な本文まで巻き込んで消えてしまう。
VOID_LIKE_TAGS = frozenset({"embed", "link", "meta"})

# `<title>` でサイト名を区切るのによく使われる文字。
TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " » ", " · ", "｜", " ｜ ")

# 属性値から除去して判定する制御文字（ブラウザは URL 解釈時にこれらを無視する）。
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x20\x7f]")

# スクリプト実行やコンテンツ差し込みにつながるスキーム。
DANGEROUS_URL_SCHEMES = ("javascript:", "vbscript:", "data:")

# これ未満の本文は記事とみなさない。日本語記事なら数百字が下限として妥当で、
# 一覧ページやエラーページを記事として取り込まないための足切りに使う。
MIN_BODY_LENGTH = 200


@dataclass(frozen=True)
class ExtractedArticle:
    """抽出済みの記事。"""

    title: str
    body: str
    canonical_url: str
    published_at: datetime | None
    language: str | None
    author: str | None
    body_hash: str


def has_dangerous_scheme(value: str) -> bool:
    """属性値がスクリプト実行につながるスキームかを判定する。

    ブラウザは URL 解釈時にタブや改行を無視するため、`jav&#9;ascript:` のような
    表記も `javascript:` として実行される。判定前に制御文字と空白を取り除く。
    """
    collapsed = _CONTROL_CHARACTERS.sub("", value).strip().lower()
    return collapsed.startswith(DANGEROUS_URL_SCHEMES)


def sanitize_html(html: str) -> str:
    """危険な要素を除去した HTML を返す。

    本文抽出は除去後の HTML に対して行う。除去前に抽出すると
    `<script>` 内の文字列が本文として混入しうる。
    """
    soup = BeautifulSoup(html, "lxml")
    for selector in DANGEROUS_TAGS:
        for element in soup.select(selector):
            if element.name in VOID_LIKE_TAGS:
                # 子は本来この要素の中身ではないため、消す前に親へ移す。
                for child in reversed(list(element.contents)):
                    element.insert_after(child.extract())
            element.decompose()

    # インラインのイベントハンドラと危険スキームの URL を落とす。
    for element in soup.find_all(True):
        for attribute in list(getattr(element, "attrs", {})):
            value = element.attrs.get(attribute)
            if attribute.lower().startswith("on"):
                del element.attrs[attribute]
            elif isinstance(value, str) and has_dangerous_scheme(value):
                del element.attrs[attribute]

    return str(soup)


def compute_body_hash(body: str) -> str:
    """本文のハッシュ。再解析の要否判定と重複判定に使う。

    空白の揺れで別物と判定されないよう正規化してからハッシュ化する。
    """
    normalized = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_published_at(value: str | None) -> datetime | None:
    """公開日を UTC の datetime に変換する。

    形式が不定のため、解釈できない場合は None を返して呼び出し側の判断に委ねる。
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # 日付のみ (YYYY-MM-DD) など ISO 8601 の部分形にも対応する。
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _extract_html_language(soup: BeautifulSoup) -> str | None:
    """`<html lang>` から原文言語を取り出す。

    本格的な言語判定は Issue #5 で行う。ここでは明示された値のみを拾う。
    """
    html_tag = soup.find("html")
    if html_tag is None:
        return None
    language = html_tag.get("lang")
    if isinstance(language, str) and language.strip():
        return language.strip()
    return None


def strip_site_suffix(title: str, site_names: Iterable[str | None]) -> str:
    """`記事名 - サイト名` からサイト名部分を落とす。

    多くのサイトが `<title>` に区切り文字でサイト名を付ける。そのまま使うと
    フィード上でどの記事も同じ見出しに見えてしまう。

    「記事名が先、サイト名が後」という一般的な並びを前提とし、**末尾**が
    既知のサイト名と一致する場合だけ落とす。一致しなければ元の値を返す。
    """
    normalized = {name.strip().casefold() for name in site_names if name and name.strip()}
    if not normalized:
        return title

    for separator in TITLE_SEPARATORS:
        if separator not in title:
            continue
        head, _, tail = title.rpartition(separator)
        if head.strip() and tail.strip().casefold() in normalized:
            return head.strip()
    return title


def _extract_title(soup: BeautifulSoup, metadata: object) -> str:
    """記事タイトルを決める。

    `og:title` が最も記事名に近い。無い場合は `<title>` からサイト名を落として使う。
    trafilatura のタイトルは区切り文字の解釈でサイト名側を拾うことがあるため、
    これらより後の候補として扱う。
    """
    trafilatura_title = getattr(metadata, "title", None) or ""

    site_names: list[str | None] = [getattr(metadata, "sitename", None)]
    og_site = soup.find("meta", property="og:site_name")
    if og_site is not None:
        content = og_site.get("content")
        if isinstance(content, str):
            site_names.append(content)
    # trafilatura が `記事名 - サイト名` からサイト名側をタイトルとして拾うことがある。
    # その値が `<title>` の末尾と一致するなら、それはサイト名だと判断できる。
    site_names.append(trafilatura_title)

    candidates: list[str] = []

    og_title = soup.find("meta", property="og:title")
    if og_title is not None:
        content = og_title.get("content")
        if isinstance(content, str):
            candidates.append(content)

    title_tag = soup.find("title")
    if title_tag is not None:
        candidates.append(title_tag.get_text(strip=True))

    candidates.append(trafilatura_title)

    h1 = soup.find("h1")
    if h1 is not None:
        candidates.append(h1.get_text(strip=True))

    for candidate in candidates:
        cleaned = strip_site_suffix(candidate.strip(), site_names).strip()
        if cleaned:
            return cleaned
    return ""


def _extract_body(sanitized_html: str, source_url: str) -> str | None:
    """本文を抽出する。取得できなければ None を返す。

    trafilatura を主に使い、失敗した場合は readability にフォールバックする。
    抽出器ごとに得意なマークアップが異なり、片方だけだと単一障害点になるため。
    """
    body = trafilatura.extract(
        sanitized_html,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        url=source_url,
    )
    if body and len(body.strip()) >= MIN_BODY_LENGTH:
        return body.strip()

    try:
        summary_html = ReadabilityDocument(sanitized_html).summary(html_partial=True)
    except Exception:
        return None

    fallback = BeautifulSoup(summary_html, "lxml").get_text(separator="\n", strip=True)
    if fallback and len(fallback) >= MIN_BODY_LENGTH:
        return fallback
    return None


def _extract_canonical_href(soup: BeautifulSoup) -> str | None:
    """`<link rel="canonical">` の href を取り出す。"""
    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag is None:
        return None
    href = canonical_tag.get("href")
    return href if isinstance(href, str) else None


def extract_article(html: str, source_url: str) -> ExtractedArticle:
    """HTML から記事の構造化データを取り出す。

    trafilatura を主に使い、本文が取れない場合は例外にする。
    """
    sanitized = sanitize_html(html)
    soup = BeautifulSoup(sanitized, "lxml")

    body = _extract_body(sanitized, source_url)
    if body is None:
        message = f"本文を抽出できませんでした: {source_url}"
        raise ExtractionError(message)

    metadata = trafilatura.extract_metadata(sanitized, default_url=source_url)
    title = _extract_title(soup, metadata)
    if not title:
        message = f"タイトルを抽出できませんでした: {source_url}"
        raise ExtractionError(message)

    # `<link>` はサニタイズで除去されるため、canonical は元の HTML から読む。
    # 使うのは href の文字列だけで、別ホストへの差し替えは resolve_canonical_url が弾く。
    canonical_href = _extract_canonical_href(BeautifulSoup(html, "lxml"))

    return ExtractedArticle(
        title=title,
        body=body.strip(),
        canonical_url=resolve_canonical_url(source_url, canonical_href),
        published_at=_parse_published_at(getattr(metadata, "date", None)),
        language=_extract_html_language(soup),
        author=(getattr(metadata, "author", None) or None),
        body_hash=compute_body_hash(body),
    )
