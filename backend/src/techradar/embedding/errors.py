"""Embedding 生成で発生するエラー。"""

from __future__ import annotations


class EmbeddingError(Exception):
    """Embedding 生成の失敗を表す基底クラス。"""

    reason: str = "embedding_failed"


class EmbeddingModelLoadError(EmbeddingError):
    """モデルを読み込めなかった。"""

    reason = "embedding_model_load_failed"


class EmbeddingDimensionMismatchError(EmbeddingError):
    """モデルの出力次元が設定と一致しない。

    DB の `vector(1024)` と食い違うと保存時に失敗するため、生成直後に検出する。
    """

    reason = "embedding_dimension_mismatch"
