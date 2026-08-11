"""外部 URL の取得と記事抽出。

外部からの入力に触れる処理をこのモジュールへ隔離する（`PROJECT_SPEC.md` §25）。
"""

from techradar.fetcher.errors import (
    ExtractionError,
    FetchError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeUrlError,
    UnsupportedContentTypeError,
)
from techradar.fetcher.extract import (
    ExtractedArticle,
    compute_body_hash,
    extract_article,
)
from techradar.fetcher.http import (
    FEED_CONTENT_TYPES,
    HTML_CONTENT_TYPES,
    JSON_CONTENT_TYPES,
    FetchedPage,
    FetchedResource,
    fetch_page,
    fetch_resource,
)
from techradar.fetcher.service import (
    IngestResult,
    find_existing_article,
    ingest_article,
)
from techradar.fetcher.ssrf import is_blocked_ip, validate_url
from techradar.fetcher.url import normalize_url, resolve_canonical_url

__all__ = [
    "FEED_CONTENT_TYPES",
    "HTML_CONTENT_TYPES",
    "JSON_CONTENT_TYPES",
    "ExtractedArticle",
    "ExtractionError",
    "FetchError",
    "FetchedPage",
    "FetchedResource",
    "IngestResult",
    "ResponseTooLargeError",
    "TooManyRedirectsError",
    "UnsafeUrlError",
    "UnsupportedContentTypeError",
    "compute_body_hash",
    "extract_article",
    "fetch_page",
    "fetch_resource",
    "find_existing_article",
    "ingest_article",
    "is_blocked_ip",
    "normalize_url",
    "resolve_canonical_url",
    "validate_url",
]
