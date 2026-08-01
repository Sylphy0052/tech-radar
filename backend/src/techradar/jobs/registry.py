"""ジョブ種別とハンドラの対応付け（`PROJECT_SPEC.md` §6）。

モジュールグローバルな可変 dict でハンドラを管理すると、テストごとに登録内容を
切り替えたい場合に互いを汚染してしまう。レジストリをインスタンス化できる形にし、
`JobWorker` へ明示的に注入することで、本番用と テスト用の登録内容を独立させる。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from techradar.db.enums import JobType


@dataclass(frozen=True)
class JobContext:
    """ジョブハンドラへ渡す値。

    ORM の `Job` インスタンスをそのまま渡さない。ハンドラは
    `asyncio.to_thread` の外側（イベントループ側）から、`Job` を取得した
    セッションとは別のライフサイクルで呼ばれるため、セッションに紐づいた
    オブジェクトを跨がせると detach 済みインスタンスへの遅延ロードアクセスで
    壊れる。ここでは必要な値だけをプレーンな型で抽出して渡す。
    """

    job_id: uuid.UUID
    job_type: JobType
    payload: dict[str, Any]
    attempts: int


# ハンドラは `JobContext` を受け取り、処理結果は例外の有無で表現する
# （戻り値を持たせない。成功/失敗の判定を戻り値の解釈に依存させないため）。
JobHandler = Callable[[JobContext], Awaitable[None]]


class JobHandlerRegistry:
    """ジョブ種別 → ハンドラの対応を保持する。"""

    def __init__(self) -> None:
        self._handlers: dict[JobType, JobHandler] = {}

    def register(self, job_type: JobType, handler: JobHandler) -> None:
        """ハンドラを登録する。

        同じ種別の二重登録は `ValueError` にする。サイレントな上書きを許すと、
        実装漏れ（別の場所で既に登録済みなのを知らずに再登録してしまう等）に
        気付けないため、登録時点で早期に失敗させる。
        """
        if job_type in self._handlers:
            message = f"job_type={job_type.value} は既に登録済みです"
            raise ValueError(message)
        self._handlers[job_type] = handler

    def get(self, job_type: JobType) -> JobHandler | None:
        """種別に対応するハンドラを返す。未登録なら `None`。"""
        return self._handlers.get(job_type)

    @property
    def registered_types(self) -> frozenset[JobType]:
        """登録済みの種別一覧（読み取り専用）。"""
        return frozenset(self._handlers.keys())


async def _crawl_sources_placeholder(context: JobContext) -> None:
    """`crawl_sources` のプレースホルダハンドラ（no-op）。

    公式ソースレジストリを巡回して候補記事を集め、後続の `fetch_article`
    ジョブを積む処理は Issue #9 の担当。本タスク（T3）はワーカーの配線を
    通すことが目的のため、ここでは何もしないハンドラだけを登録しておく。
    """
    return None


def create_default_registry() -> JobHandlerRegistry:
    """既定のレジストリを返す。

    `crawl_sources` のみを登録する。`fetch_article` / `analyze_article` /
    `embed_article` / `generate_feed` はまだハンドラの実装がない後続タスクの
    担当のため、あえて登録しない。未登録種別のジョブが enqueue された場合、
    ワーカー側で検出してリトライせず即 failed にできるようにするため
    （登録漏れを握りつぶさない）。
    """
    registry = JobHandlerRegistry()
    registry.register(JobType.CRAWL_SOURCES, _crawl_sources_placeholder)
    return registry
