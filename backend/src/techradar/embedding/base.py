"""Embedding プロバイダーの抽象。

MVP では Qwen3-Embedding-0.6B をローカル実行するが、実装を差し替えられるよう
プロトコルで定義する（`PROJECT_SPEC.md` §25「Embeddingモデルを交換可能にする」）。

DB の `vector(1024)` と食い違うと保存時に失敗するため、次元は
`techradar.db.EMBEDDING_DIMENSIONS` を単一の情報源として検証する。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from techradar.db import EMBEDDING_DIMENSIONS
from techradar.embedding.errors import EmbeddingDimensionMismatchError

# Qwen3-Embedding は用途ごとに前置きを付けると精度が上がる。
# 検索クエリ側と文書側で別の前置きを使う。
QUERY_PROMPT_NAME = "query"


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding プロバイダーが満たすべきインターフェース。"""

    name: str
    dimensions: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """保存対象の文書を埋め込む。"""
        ...

    def embed_query(self, text: str) -> list[float]:
        """検索クエリを埋め込む。"""
        ...


def assert_dimensions(vectors: Sequence[Sequence[float]]) -> None:
    """出力次元が DB スキーマと一致することを確認する。

    モデルを差し替えた際の取り違えを、保存より手前で検出する。
    """
    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSIONS:
            message = (
                f"Embedding の次元が一致しません: 期待 {EMBEDDING_DIMENSIONS}, 実際 {len(vector)}"
            )
            raise EmbeddingDimensionMismatchError(message)
