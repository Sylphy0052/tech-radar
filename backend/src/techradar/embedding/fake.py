"""テストで使う `EmbeddingProvider` の代替実装。

実モデルを読み込まずに、決定的で**互いに十分離れた**ベクトルを返す。
`EmbeddingProvider` が差し替え可能な抽象になっていることを示す役割も持つ。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence

from techradar.db import EMBEDDING_DIMENSIONS


class FakeEmbeddingProvider:
    """文字列から決定的にベクトルを作るプロバイダー。

    同じ文字列は常に同じベクトルになる。異なる文字列は**ほぼ直交する**
    ベクトルになり、近傍検索の順序を検証できる。

    ハッシュを 1024 次元へ並べ直すだけだと、どの文字列も同じ方向を向いた
    非負ベクトルになり、無関係な文字列同士でもコサイン類似度が 0.8 前後に
    なってしまう（実測）。ハッシュを乱数の種にして正負両方の値を作ることで
    この偏りを避ける。

    意味的な近さは持たない。実モデルの検証は `test_embedding_qwen.py`。
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
        """文字列のハッシュを種にした、正規化済みのランダムベクトル。"""
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        generator = random.Random(seed)  # noqa: S311 — 暗号用途ではなくテスト用の再現可能な擬似乱数
        raw = [generator.gauss(0.0, 1.0) for _ in range(self.dimensions)]
        norm = math.sqrt(sum(value * value for value in raw)) or 1.0
        return [value / norm for value in raw]
