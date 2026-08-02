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

from techradar.config import Settings, get_settings
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


def create_default_registry(settings: Settings | None = None) -> JobHandlerRegistry:
    """既定のレジストリを返す。

    `crawl_sources` / `fetch_article` / `analyze_article` / `embed_article` は
    巡回から URL 登録の end-to-end（Issue #9, Issue #12 T3）までを成立させる
    ハンドラを登録する。`generate_feed` / `deduplicate_articles` はまだハンドラの
    実装がない後続タスクの担当のため、あえて登録しない。`purge_operation_logs`
    は巡回の完了時に積まれる保守ジョブ（Issue #19）として登録する。
    `purge_recommendation_runs` も同様に巡回の完了時に積まれる保守ジョブ
    （Issue #28）として登録する。`rebuild_interest_clusters` は記事フィードバック
    （`api/feedback.py`）のたびに積まれる関心クラスタ再構築ジョブ（Issue #15）
    として登録する。未登録種別のジョブが
    enqueue された場合、ワーカー側で検出してリトライせず即 failed に
    できるようにするため（登録漏れを握りつぶさない）。

    `techradar.jobs.handlers` 配下のモジュールは型注釈で `JobContext` /
    `JobHandler` を参照するためこのモジュールを import し返す。関数内で
    import することで、モジュール読み込み時点の循環 import を避ける。
    """
    resolved_settings = settings or get_settings()

    from techradar.jobs.handlers import (
        make_analyze_article_handler,
        make_crawl_sources_handler,
        make_embed_article_handler,
        make_fetch_article_handler,
        make_purge_operation_logs_handler,
        make_purge_recommendation_runs_handler,
        make_rebuild_interest_clusters_handler,
    )

    registry = JobHandlerRegistry()
    registry.register(JobType.CRAWL_SOURCES, make_crawl_sources_handler(resolved_settings))
    registry.register(JobType.FETCH_ARTICLE, make_fetch_article_handler(resolved_settings))
    registry.register(JobType.ANALYZE_ARTICLE, make_analyze_article_handler(resolved_settings))
    registry.register(JobType.EMBED_ARTICLE, make_embed_article_handler(resolved_settings))
    registry.register(
        JobType.PURGE_OPERATION_LOGS, make_purge_operation_logs_handler(resolved_settings)
    )
    registry.register(
        JobType.PURGE_RECOMMENDATION_RUNS,
        make_purge_recommendation_runs_handler(resolved_settings),
    )
    registry.register(
        JobType.REBUILD_INTEREST_CLUSTERS,
        make_rebuild_interest_clusters_handler(resolved_settings),
    )
    return registry
