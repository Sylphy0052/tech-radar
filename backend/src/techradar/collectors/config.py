"""候補記事コレクターの巡回設定ファイル読み込み（`PROJECT_SPEC.md` §12, §25）。

巡回先（公式 RSS/Atom、国内技術メディア、GitHub Releases、arXiv カテゴリ、
Hacker News）をコードに埋め込まず `config/feeds.yaml` で管理する。読み込み時に
Pydantic で検証し、壊れた設定のまま巡回を始めないようにする。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# backend/src/techradar/collectors/config.py から 3 階層上が backend/
BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = BACKEND_ROOT / "config" / "feeds.yaml"

# 直近何日以内を候補に残すかの下限。0 以下だとフィルタが機能しない。
MIN_FRESHNESS_DAYS = 1
# 1 回の巡回で enqueue する上限の下限。0 以下だと何も enqueue できない。
MIN_CANDIDATES_PER_RUN = 1
# Hacker News から拾う件数の下限・上限。上限は API 呼び出し量とコストの安全弁。
MIN_HACKER_NEWS_TOP_ITEMS = 1
MAX_HACKER_NEWS_TOP_ITEMS = 500

# GitHub のリポジトリ名（owner・repo とも）は英数字・ハイフン・アンダースコア・
# ドットのみで構成される。それ以外の文字を含む場合は設定ミスとみなし、
# owner/repo ちょうど 2 セグメントのみ許可する（パス injection 的な値を弾く）。
GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# arXiv のカテゴリ表記（例: cs.SE）は英数字とドットのみで構成される。
ARXIV_CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9.]+$")


class CollectorConfigError(Exception):
    """設定ファイルを読み込めなかった場合のエラー。"""


class FeedEntryConfig(BaseModel):
    """RSS/Atom フィード 1 件分の設定。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: str

    @field_validator("url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        # http は通信経路上で盗聴・改ざんされうる。公式フィードであっても
        # https 以外は設定ミスとして起動時（読み込み時）に弾く。
        if not value.startswith("https://"):
            message = f"フィード URL は https のみ許可します: {value}"
            raise ValueError(message)
        return value


class FeedsConfig(BaseModel):
    """`config/feeds.yaml` 全体。"""

    model_config = ConfigDict(extra="forbid")

    freshness_days: int = Field(ge=MIN_FRESHNESS_DAYS)
    max_candidates_per_run: int = Field(ge=MIN_CANDIDATES_PER_RUN)
    rss: list[FeedEntryConfig] = Field(default_factory=list)
    jp_media: list[FeedEntryConfig] = Field(default_factory=list)
    github_repositories: list[str] = Field(default_factory=list)
    arxiv_categories: list[str] = Field(default_factory=list)
    hacker_news_top_items: int = Field(ge=MIN_HACKER_NEWS_TOP_ITEMS, le=MAX_HACKER_NEWS_TOP_ITEMS)

    @field_validator("github_repositories")
    @classmethod
    def _validate_github_repositories(cls, value: list[str]) -> list[str]:
        for repository in value:
            if not GITHUB_REPOSITORY_PATTERN.fullmatch(repository):
                message = f"GitHub リポジトリは owner/repo 形式のみ許可します: {repository}"
                raise ValueError(message)
        return value

    @field_validator("arxiv_categories")
    @classmethod
    def _validate_arxiv_categories(cls, value: list[str]) -> list[str]:
        for category in value:
            if not ARXIV_CATEGORY_PATTERN.fullmatch(category):
                message = f"arXiv カテゴリは英数字とドットのみ許可します: {category}"
                raise ValueError(message)
        return value


def load_feeds_config(path: Path | None = None) -> FeedsConfig:
    """設定ファイルを読み込んで検証する。

    Raises:
        CollectorConfigError: ファイルが無い、YAML として壊れている、
            またはマッピング以外が書かれている場合。
    """
    resolved = path or DEFAULT_CONFIG_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"巡回設定を読み込めません: {resolved}"
        raise CollectorConfigError(message) from exc

    try:
        # 任意のオブジェクトを構築しないよう safe_load を使う。
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        message = f"巡回設定の YAML が不正です: {resolved}"
        raise CollectorConfigError(message) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        message = f"巡回設定はマッピングである必要があります: {resolved}"
        raise CollectorConfigError(message)

    return FeedsConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_feeds_config() -> FeedsConfig:
    """同梱設定のシングルトンを返す。

    設定ファイルは巡回中に変わらないため、コレクターごとに読み直さない。
    テストで差し替える場合は `get_feeds_config.cache_clear()` を呼ぶ。
    """
    return load_feeds_config()
