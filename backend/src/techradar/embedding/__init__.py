"""Embedding 層。

モデルを交換可能にするため、生成処理をこのパッケージへ隔離する
（`PROJECT_SPEC.md` §25）。
"""

from techradar.embedding.base import EmbeddingProvider, assert_dimensions
from techradar.embedding.errors import (
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingModelLoadError,
)
from techradar.embedding.fake import FakeEmbeddingProvider
from techradar.embedding.qwen import QwenEmbeddingProvider, resolve_device
from techradar.embedding.service import (
    EmbedResult,
    embed_articles,
    find_neighbours,
    find_similar_articles,
)

__all__ = [
    "EmbedResult",
    "EmbeddingDimensionMismatchError",
    "EmbeddingError",
    "EmbeddingModelLoadError",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "QwenEmbeddingProvider",
    "assert_dimensions",
    "embed_articles",
    "find_neighbours",
    "find_similar_articles",
    "resolve_device",
]
