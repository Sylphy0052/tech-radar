"""巡回設定ファイルの読み込みを検証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from techradar.collectors.config import (
    DEFAULT_CONFIG_PATH,
    CollectorConfigError,
    load_feeds_config,
)


@pytest.fixture(scope="module")
def config():
    """同梱の `config/feeds.yaml`。"""
    return load_feeds_config()


class TestLoading:
    def test_loads_the_bundled_feeds_config(self, config):
        # Arrange / Act / Assert — 同梱ファイルが常に読める（巡回開始時に落ちない）
        assert DEFAULT_CONFIG_PATH.exists()
        assert config.freshness_days >= 1
        assert config.max_candidates_per_run >= 1

    def test_reads_each_section(self, config):
        # Arrange / Act / Assert — 仕様の各セクションが読める
        assert len(config.rss) >= 1
        assert len(config.jp_media) >= 1
        assert len(config.github_repositories) >= 1
        assert len(config.arxiv_categories) >= 1
        assert config.hacker_news_top_items >= 1

        first_feed = config.rss[0]
        assert first_feed.name
        assert first_feed.url.startswith("https://")

    def test_rejects_a_non_https_feed_url(self, tmp_path: Path):
        # Arrange — 平文 http は盗聴・改ざんの余地があるため設定ミスとして弾く
        path = tmp_path / "feeds.yaml"
        path.write_text(
            "freshness_days: 7\n"
            "max_candidates_per_run: 200\n"
            "rss:\n"
            "  - name: Example\n"
            "    url: http://example.com/feed.xml\n"
            "hacker_news_top_items: 50\n",
            encoding="utf-8",
        )

        # Act / Assert
        with pytest.raises(ValueError, match="https"):
            load_feeds_config(path)

    def test_rejects_a_github_repository_not_in_owner_repo_form(self, tmp_path: Path):
        # Arrange — owner/repo 形式以外は設定ミスとして弾く
        path = tmp_path / "feeds.yaml"
        path.write_text(
            "freshness_days: 7\n"
            "max_candidates_per_run: 200\n"
            "github_repositories:\n"
            "  - kubernetes\n"
            "hacker_news_top_items: 50\n",
            encoding="utf-8",
        )

        # Act / Assert
        with pytest.raises(ValueError, match="owner/repo"):
            load_feeds_config(path)

    def test_rejects_an_unknown_key(self, tmp_path: Path):
        # Arrange — 設定漏れに気付けるよう未知のキーを許さない
        path = tmp_path / "feeds.yaml"
        path.write_text("freshness_dayss: 7\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="freshness_dayss"):
            load_feeds_config(path)

    def test_reports_a_missing_file(self, tmp_path: Path):
        # Arrange / Act / Assert
        with pytest.raises(CollectorConfigError, match="読み込めません"):
            load_feeds_config(tmp_path / "absent.yaml")

    def test_reports_broken_yaml(self, tmp_path: Path):
        # Arrange
        path = tmp_path / "feeds.yaml"
        path.write_text("rss: [\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(CollectorConfigError, match="YAML"):
            load_feeds_config(path)

    def test_reports_a_non_mapping_document(self, tmp_path: Path):
        # Arrange
        path = tmp_path / "feeds.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")

        # Act / Assert
        with pytest.raises(CollectorConfigError, match="マッピング"):
            load_feeds_config(path)

    def test_rejects_a_non_positive_freshness_days(self, tmp_path: Path):
        # Arrange — 0 以下だと直近フィルタが機能しない
        path = tmp_path / "feeds.yaml"
        path.write_text(
            "freshness_days: 0\nmax_candidates_per_run: 200\nhacker_news_top_items: 50\n",
            encoding="utf-8",
        )

        # Act / Assert
        with pytest.raises(ValueError, match="freshness_days"):
            load_feeds_config(path)
