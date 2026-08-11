"""URL 正規化を検証する（重複排除の前提）。"""

from __future__ import annotations

import pytest

from techradar.fetcher.url import (
    is_tracking_parameter,
    normalize_url,
    resolve_canonical_url,
)


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(
                "HTTPS://Example.COM/Article",
                "https://example.com/Article",
                id="lowercases-scheme-and-host-but-keeps-path-case",
            ),
            pytest.param(
                "https://example.com:443/a",
                "https://example.com/a",
                id="drops-default-https-port",
            ),
            pytest.param(
                "http://example.com:80/a",
                "http://example.com/a",
                id="drops-default-http-port",
            ),
            pytest.param(
                "https://example.com:8443/a",
                "https://example.com:8443/a",
                id="keeps-non-default-port",
            ),
            pytest.param(
                "https://example.com/a#section",
                "https://example.com/a",
                id="drops-fragment",
            ),
            pytest.param(
                "https://example.com/a/",
                "https://example.com/a",
                id="drops-trailing-slash",
            ),
            pytest.param(
                "https://example.com",
                "https://example.com/",
                id="keeps-root-slash",
            ),
            pytest.param(
                "https://example.com/a?b=2&a=1",
                "https://example.com/a?a=1&b=2",
                id="sorts-query-parameters",
            ),
            pytest.param(
                "https://example.com/a?utm_source=x&utm_medium=y&id=1",
                "https://example.com/a?id=1",
                id="drops-utm-parameters",
            ),
            pytest.param(
                "https://example.com/a?fbclid=x&gclid=y&id=1",
                "https://example.com/a?id=1",
                id="drops-click-ids",
            ),
        ],
    )
    def test_normalizes(self, raw: str, expected: str):
        # Arrange / Act / Assert
        assert normalize_url(raw) == expected

    def test_treats_query_order_and_tracking_variants_as_the_same_article(self):
        # Arrange — 同じ記事を指す 3 つの表記
        variants = [
            "https://example.com/posts/1?a=1&b=2",
            "https://example.com/posts/1/?b=2&a=1#top",
            "HTTPS://Example.com:443/posts/1?b=2&utm_campaign=z&a=1",
        ]

        # Act
        normalized = {normalize_url(url) for url in variants}

        # Assert
        assert len(normalized) == 1

    def test_is_idempotent(self):
        # Arrange
        url = "https://example.com/a/?b=2&utm_source=x&a=1#frag"

        # Act
        once = normalize_url(url)
        twice = normalize_url(once)

        # Assert
        assert once == twice


class TestIsTrackingParameter:
    @pytest.mark.parametrize(
        "name", ["utm_source", "UTM_Medium", "fbclid", "gclid", "mc_cid", "pk_campaign"]
    )
    def test_detects_tracking_parameters(self, name: str):
        # Arrange / Act / Assert
        assert is_tracking_parameter(name) is True

    @pytest.mark.parametrize("name", ["id", "page", "q", "lang"])
    def test_keeps_meaningful_parameters(self, name: str):
        # Arrange / Act / Assert
        assert is_tracking_parameter(name) is False


class TestResolveCanonicalUrl:
    def test_uses_relative_canonical(self):
        # Arrange / Act
        resolved = resolve_canonical_url("https://example.com/a?utm_source=x", "/posts/1")

        # Assert
        assert resolved == "https://example.com/posts/1"

    def test_falls_back_to_source_url_when_canonical_is_absent(self):
        # Arrange / Act
        resolved = resolve_canonical_url("https://example.com/a?utm_source=x", None)

        # Assert
        assert resolved == "https://example.com/a"

    def test_ignores_canonical_pointing_to_another_host(self):
        # Arrange — 他サイトを canonical に指定して記事を乗っ取る手口を防ぐ
        resolved = resolve_canonical_url("https://example.com/a", "https://evil.example.net/a")

        # Assert
        assert resolved == "https://example.com/a"
