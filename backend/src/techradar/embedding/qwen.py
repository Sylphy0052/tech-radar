"""Qwen3-Embedding-0.6B をローカル実行する `EmbeddingProvider` 実装。

追加課金なしで多言語・クロスリンガルの類似度を得るため、ローカル GPU で動かす
（ADR 0001）。GPU が使えない環境では CPU へ自動フォールバックする。

モデルはプロセス内で 1 度だけ読み込む。読み込みに数秒かかるため、
呼び出しごとに読み直すと実用にならない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from techradar.config import Settings, get_settings
from techradar.embedding.base import assert_dimensions
from techradar.embedding.errors import EmbeddingModelLoadError

if TYPE_CHECKING:  # pragma: no cover - 型チェック時のみ
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Qwen3-Embedding は検索クエリ側に指示文を付けると精度が上がる。
# 文書側には付けない（モデルカードの推奨に従う）。
QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"


def is_cuda_available() -> bool:
    """CUDA が使えるかを返す。

    torch の import は重く、ドライバの状態にも左右される。判定をこの関数に
    閉じ込めることで、呼び出し側とテストが torch へ直接依存せずに済む。
    """
    try:
        import torch
    except ImportError:  # pragma: no cover - torch は必須依存
        return False

    return bool(torch.cuda.is_available())


def resolve_device(
    configured: str,
    *,
    cuda_available: Callable[[], bool] = is_cuda_available,
) -> str:
    """使用するデバイスを決める。

    `auto` のときは CUDA が使えれば CUDA、なければ CPU を選ぶ。
    判定は引数で差し替えられるようにし、テストが torch を読み込まずに済むようにする。
    """
    if configured != "auto":
        return configured
    return "cuda" if cuda_available() else "cpu"


@lru_cache(maxsize=1)
def load_model(model_name: str, device: str, max_length: int) -> SentenceTransformer:
    """モデルを読み込む。プロセス内で 1 度だけ実行される。"""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - 依存が入っていれば起きない
        message = "sentence-transformers が利用できません"
        raise EmbeddingModelLoadError(message) from exc

    logger.info("Embedding モデルを読み込みます: %s (device=%s)", model_name, device)
    try:
        model = SentenceTransformer(model_name, device=device)
    except Exception as exc:
        message = f"Embedding モデルを読み込めません: {model_name}"
        raise EmbeddingModelLoadError(message) from exc

    # 記事本文を丸ごと入れると attention のメモリが跳ねるため上限を設ける。
    model.max_seq_length = max_length
    return model


class QwenEmbeddingProvider:
    """Qwen3-Embedding-0.6B をローカルで実行するプロバイダー。"""

    name = "qwen3-embedding"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.dimensions = self._settings.embedding_dimensions
        self._device = resolve_device(self._settings.embedding_device)

    @property
    def device(self) -> str:
        """実際に使用するデバイス。"""
        return self._device

    def _model(self) -> Any:
        return load_model(
            self._settings.embedding_model,
            self._device,
            self._settings.embedding_max_length,
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文書を埋め込む。空の入力ではモデルを読み込まない。"""
        if not texts:
            return []

        vectors = self._model().encode(
            list(texts),
            batch_size=self._settings.embedding_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        result = [[float(value) for value in vector] for vector in vectors]
        assert_dimensions(result)
        return result

    def embed_query(self, text: str) -> list[float]:
        """検索クエリを埋め込む。

        文書側と違い、クエリには指示文を前置きする（モデルカードの推奨）。
        """
        vector = self._model().encode(
            [text],
            batch_size=1,
            normalize_embeddings=True,
            show_progress_bar=False,
            prompt=f"Instruct: {QUERY_INSTRUCTION}\nQuery: ",
        )[0]
        result = [float(value) for value in vector]
        assert_dimensions([result])
        return result
