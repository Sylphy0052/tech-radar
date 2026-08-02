"""候補記事コレクターの抽象（`PROJECT_SPEC.md` §12）。

巡回先（公式 RSS/Atom、国内技術メディア、GitHub Releases、arXiv、Hacker News など）を
差し替え・追加しやすくするため、収集処理をプロトコルで定義する。実際の巡回設定は
`techradar.collectors.config` が、収集結果の enqueue やエラー時の扱いは後続タスクの
service 層が受け持つ。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


class CollectorError(Exception):
    """収集処理の失敗を表す基底クラス。"""


@dataclass(frozen=True)
class CandidateArticle:
    """巡回で見つかった候補記事 1 件。

    `url` は正規化前の値をそのまま保持する。正規化・重複排除の前段処理は
    `techradar.fetcher.url.normalize_url` が後続処理として担うため、ここでは
    フィードやページから読み取った値をそのまま渡してよい。
    """

    url: str
    title: str
    # タイムゾーン付き UTC。取得できないフィードでは None とし、
    # 後続の直近 N 日フィルタ（`PROJECT_SPEC.md` §12）で除外させる。
    published_at: datetime | None
    # どのコレクターが見つけたか。ログ・デバッグ用で、判定ロジックには使わない。
    collector_name: str
    # フィード側が持つ情報源名など。無ければ None のままでよい。
    source_hint: str | None = None


@runtime_checkable
class SourceCollector(Protocol):
    """候補記事コレクターが満たすべきインターフェース。

    1 つのコレクターの収集失敗が巡回全体を止めてはならない。呼び出し側
    （後続タスクの service 層）は `collect()` が送出する `CollectorError` を
    コレクター単位で捕捉し、そのコレクターだけスキップして他のコレクターの
    収集を継続する。このプロトコル自体はエラーを握りつぶさず、失敗をそのまま
    呼び出し元へ伝播させる（黙って空リストを返さない）。
    """

    name: str

    def collect(self) -> Sequence[CandidateArticle]:
        """巡回先から候補記事を収集する。

        HTTP 通信は `techradar.fetcher` 経由の同期呼び出しで行う（巡回は
        UI の実行ボタンから都度起動する前提のため、非同期化は必須ではない）。

        Raises:
            CollectorError: 収集に失敗した場合。
        """
        ...
