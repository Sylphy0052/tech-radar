"""GitHub Releases からの候補記事収集（`PROJECT_SPEC.md` §12）。

GitHub REST API（`https://api.github.com/repos/<owner>/<repo>/releases`）を使う。
HTTP 通信は必ず `techradar.fetcher.http.fetch_resource` 経由で行う。SSRF ガード
（DNS ピンニング・リダイレクト検証・レスポンスサイズ上限）を迂回しないためで、
`httpx` を直接使ったり自前で名前解決したりしてはならない。

`Settings.github_token` が設定されていれば `Authorization: Bearer <token>` を
付けて認証済みのレート制限で取得する。未設定でも従来どおり未認証で動作する。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from techradar.collectors.base import CandidateArticle
from techradar.collectors.config import FeedsConfig, get_feeds_config
from techradar.config import Settings, get_settings
from techradar.fetcher.errors import FetchError
from techradar.fetcher.http import JSON_CONTENT_TYPES, fetch_resource

logger = logging.getLogger(__name__)

GITHUB_API_BASE_URL = "https://api.github.com"

# 1 リポジトリあたりに取得する release 件数。巡回のたびに古いリリースまで
# 毎回読み直さないよう、直近分だけに絞る。
RELEASES_PER_REPOSITORY = 5

# 1 リポジトリの release 一覧取得・パースで想定される例外。ネットワーク失敗
# （FetchError）と、応答 JSON が壊れている・期待した構造でない場合
# （JSONDecodeError, KeyError, TypeError, ValueError）の両方を含む。外部 API の
# 応答を信用せず、1 リポジトリの失敗で他のリポジトリの収集を止めない。
_REPOSITORY_ERRORS = (FetchError, json.JSONDecodeError, KeyError, TypeError, ValueError)


def _build_auth_headers(github_token: str | None) -> dict[str, str]:
    """`github_token` が設定されていれば `Authorization` ヘッダを組み立てる。

    未設定時は空の dict を返す（`fetch_resource` は追加ヘッダ無しの通常の
    未認証リクエストとして扱う）。トークン値はこの関数の戻り値以外には
    渡さないため、ログや例外メッセージに出ることはない。
    """
    if github_token is None:
        return {}
    return {"Authorization": f"Bearer {github_token}"}


class GitHubReleasesCollector:
    """GitHub Releases から候補記事を収集する。"""

    name = "github_releases"

    def __init__(
        self,
        *,
        feeds_config: FeedsConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._feeds_config = feeds_config
        self._settings = settings

    def collect(self) -> Sequence[CandidateArticle]:
        """`FeedsConfig.github_repositories` の各リポジトリから候補記事を集める。

        1 リポジトリの取得失敗は他のリポジトリの収集を巻き込まない
        （`_fetch_releases` 内で捕捉してログに残し、空リストとして継続する）。
        """
        feeds_config = self._feeds_config or get_feeds_config()
        settings = self._settings or get_settings()

        candidates: list[CandidateArticle] = []
        for repository in feeds_config.github_repositories:
            candidates.extend(self._fetch_releases(repository, settings))
        return candidates

    def _fetch_releases(self, repository: str, settings: Settings) -> list[CandidateArticle]:
        """1 リポジトリの release 一覧を取得する。失敗したらログに残し空リストを返す。

        GitHub 公式は `Accept: application/vnd.github+json` を推奨するが、それを
        指定すると `fetch_resource` が Content-Type チェックに使う
        `allowed_content_types=JSON_CONTENT_TYPES`（`application/json` 限定）と
        矛盾する可能性を検証できなかった（応答 Content-Type が確実に
        `application/json` のままである保証が取れなかったため）。安全側に倒し、
        Accept は `fetch_resource` の既定（`allowed_content_types` 由来）に任せる。
        """
        url = (
            f"{GITHUB_API_BASE_URL}/repos/{repository}/releases?per_page={RELEASES_PER_REPOSITORY}"
        )
        try:
            resource = fetch_resource(
                url,
                allowed_content_types=JSON_CONTENT_TYPES,
                headers=_build_auth_headers(settings.github_token),
                settings=settings,
            )
            payload: Any = json.loads(resource.body)
        except _REPOSITORY_ERRORS as exc:
            logger.warning("GitHub Releases %s の取得をスキップします: %s", repository, exc)
            return []

        if not isinstance(payload, list):
            logger.warning("GitHub Releases %s の応答が配列ではありません", repository)
            return []

        candidates: list[CandidateArticle] = []
        for release in payload:
            candidate = self._to_candidate(repository, release)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _to_candidate(self, repository: str, release: Any) -> CandidateArticle | None:
        if not isinstance(release, dict):
            return None

        # draft / prerelease は正式リリースではないため一次情報として扱わない。
        if release.get("draft") or release.get("prerelease"):
            return None

        url = release.get("html_url")
        if not isinstance(url, str) or not url:
            return None

        # release の name は空文字のことがあるため tag_name にフォールバックする。
        name = release.get("name")
        tag_name = release.get("tag_name")
        release_label = name if isinstance(name, str) and name.strip() else tag_name
        if not isinstance(release_label, str) or not release_label.strip():
            return None

        return CandidateArticle(
            url=url,
            title=f"{repository} {release_label}",
            published_at=self._parse_published_at(release),
            collector_name=self.name,
            source_hint=repository,
        )

    @staticmethod
    def _parse_published_at(release: dict[str, Any]) -> datetime | None:
        """`published_at` を優先し、無ければ `created_at` を使う。どちらも無ければ None。"""
        for key in ("published_at", "created_at"):
            value = release.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed.astimezone(UTC)
        return None
