"""計測エントリポイントの共通補助関数（`techradar.measure.cli_support`）のテスト（Issue #73）。

`run_llm_latency.py` と `run_truncation_impact.py` の両方で使う `truncate_url()` の
テストをここへ集約する。以前は両エントリポイントのテストファイルにそれぞれ同じ内容の
テストクラスが存在した。
"""

from __future__ import annotations

from techradar.measure.cli_support import truncate_url


class TestTruncateUrl:
    def test_keeps_short_urls_unchanged(self) -> None:
        assert truncate_url("https://example.com/a") == "https://example.com/a"

    def test_truncates_long_urls_with_an_ellipsis(self) -> None:
        """記事一覧の表示行を潰さないよう、長い URL は appendix を省略する。"""
        url = "https://example.com/" + "a" * 100

        truncated = truncate_url(url, limit=20)

        assert len(truncated) == 20
        assert truncated.endswith("…")
