"""国内技術メディアの巡回コレクター（`PROJECT_SPEC.md` §12）。

Zenn・Qiita・はてなブックマークはいずれも RSS/Atom を配信しており、巡回の
実体は `techradar.collectors.rss.RssCollector` と完全に同じ（取得は
`fetch_resource` 経由、パースは `feedparser`）。パース処理を複製しないよう、
`RssCollector` を継承して `name` だけを差し替える。
"""

from __future__ import annotations

from techradar.collectors.rss import RssCollector


class JpMediaCollector(RssCollector):
    """Zenn / Qiita / はてなブックマークの RSS を巡回するコレクター。

    巡回対象は `FeedsConfig.jp_media`（どのフィードを渡すかは呼び出し側の
    責務）。パース・変換ロジックは `RssCollector` のものをそのまま使う。
    """

    name: str = "jp_media"
