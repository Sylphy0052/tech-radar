"""テストで使う `EmbeddingProvider` の代替実装。

実モデルを読み込まずに、決定的で意味のあるベクトルを返す。
`EmbeddingProvider` が差し替え可能な抽象になっていることを示す役割も持つ。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from techradar.db import EMBEDDING_DIMENSIONS


class FakeEmbeddingProvider:
    """文字列から決定的にベクトルを作るプロバイダー。

    同じ文字列は常に同じベクトルになり、異なる文字列は異なるベクトルになる。
    保存・近傍検索の配線を検証する用途に使う。意味的な近さは持たない。
    """

    name = "fake"

    def __init__(self, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self.dimensions = dimensions
        self.embedded_documents: list[str] = []
        self.embedded_queries: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文書を埋め込む。"""
        self.embedded_documents.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """検索クエリを埋め込む。"""
        self.embedded_queries.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        """文字列のハッシュから正規化済みベクトルを作る。"""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[index % len(digest)] / 255.0 for index in range(self.dimensions)]
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]
