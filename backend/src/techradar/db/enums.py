"""DB に保存する列挙値。

DB 上は text 列として保持し、アプリ側でこの列挙を使って検証する。
新しい値の追加でマイグレーションが必要にならないようにするため。
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """ジョブおよび記事登録の処理状態（`PROJECT_SPEC.md` §6.2）。"""

    PENDING = "pending"
    FETCHING = "fetching"
    ANALYZING = "analyzing"
    SEARCHING = "searching"
    COMPLETED = "completed"
    FAILED = "failed"


class JobType(StrEnum):
    """ジョブ種別。"""

    FETCH_ARTICLE = "fetch_article"
    ANALYZE_ARTICLE = "analyze_article"
    EMBED_ARTICLE = "embed_article"
    CRAWL_SOURCES = "crawl_sources"
    GENERATE_FEED = "generate_feed"
    DEDUPLICATE_ARTICLES = "deduplicate_articles"
    PURGE_OPERATION_LOGS = "purge_operation_logs"


class ArticleOrigin(StrEnum):
    """関心記事に追加された経路（`PROJECT_SPEC.md` §7.1 の重み表に対応）。"""

    MANUAL = "manual"
    GOOD = "good"
    SAVED = "saved"
    READ_FULL = "read_full"
    CLICKED = "clicked"


class FeedbackAction(StrEnum):
    """記事へのフィードバック種別。"""

    GOOD = "good"
    BAD = "bad"
    SAVE = "save"


class BadReason(StrEnum):
    """Bad の理由（`PROJECT_SPEC.md` §7.2）。任意項目。"""

    NOT_INTERESTED = "not_interested"
    TOO_SHALLOW = "too_shallow"
    ALREADY_KNOWN = "already_known"
    PROMOTIONAL = "promotional"
    UNTRUSTED_SOURCE = "untrusted_source"
    TOO_REPETITIVE = "too_repetitive"


class ContentType(StrEnum):
    """記事の性質（`PROJECT_SPEC.md` §9）。"""

    CONCEPT = "concept"
    IMPLEMENTATION = "implementation"
    RESEARCH = "research"
    NEWS = "news"


class Difficulty(StrEnum):
    """記事の難易度。"""

    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class SourceType(StrEnum):
    """情報源の種別（`PROJECT_SPEC.md` §10 の Tier 分類に対応）。"""

    OFFICIAL_DOCUMENTATION = "official_documentation"
    API_SPECIFICATION = "api_specification"
    STANDARD_SPECIFICATION = "standard_specification"
    OFFICIAL_RELEASE_NOTES = "official_release_notes"
    OFFICIAL_BLOG = "official_blog"
    OFFICIAL_RESEARCH = "official_research"
    ORIGINAL_PAPER = "original_paper"
    OFFICIAL_GITHUB_RELEASE = "official_github_release"
    COMPANY_TECH_BLOG = "company_tech_blog"
    MAINTAINER_ARTICLE = "maintainer_article"
    PERSONAL_ARTICLE = "personal_article"
    TECH_MEDIA = "tech_media"
    NEWS_REPOST = "news_repost"
    SUMMARY_REPOST = "summary_repost"
    UNKNOWN = "unknown"


class RecommendationMode(StrEnum):
    """推薦モード（`PROJECT_SPEC.md` §13）。"""

    ARTICLE_BASED = "article_based"
    DISCOVER = "discover"
