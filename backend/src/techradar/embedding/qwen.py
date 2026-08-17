"""Qwen3-Embedding-0.6B をローカル実行する `EmbeddingProvider` 実装。

追加課金なしで多言語・クロスリンガルの類似度を得るため、ローカル GPU で動かす
（ADR 0001）。GPU が使えない環境では CPU へ自動フォールバックする。

モデルはプロセス内で 1 度だけ読み込む。読み込みに数秒かかるため、
呼び出しごとに読み直すと実用にならない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import cached_property, lru_cache
from typing import TYPE_CHECKING

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


def is_xpu_available() -> bool:
    """Intel GPU（XPU）が使えるかを返す。

    `torch.xpu` は PyTorch 2.5 以降で追加されたモジュールで、古い torch や
    XPU ビルドでない torch には存在しない。属性の有無を確認してから呼び出し、
    無ければ例外を投げず False を返す。
    is_cuda_available と同様に torch の import をこの関数に閉じ込め、
    呼び出し側とテストが torch へ直接依存せずに済むようにする。
    """
    try:
        import torch
    except ImportError:  # pragma: no cover - torch は必須依存
        return False

    xpu = getattr(torch, "xpu", None)
    if xpu is None:
        return False
    return bool(xpu.is_available())


def resolve_device(
    configured: str,
    *,
    cuda_available: Callable[[], bool] = is_cuda_available,
    xpu_available: Callable[[], bool] = is_xpu_available,
) -> str:
    """使用するデバイスを決める。

    `auto` のときは CUDA → XPU → CPU の順で使えるものを選ぶ。NVIDIA の
    単体GPU（dGPU）がある環境では、Intel の統合GPU（XPU）より高速なため
    CUDA を優先する。現行の開発機は NVIDIA GPU を持たず Intel Arc
    Graphics（Core Ultra 7 165H の統合GPU）のみを持つため、実際には CUDA
    判定が False になり XPU が選ばれる。NVIDIA GPU を積んだ機体へ戻した
    ときに備え、判定の順序自体は CUDA を先に見る形を維持する。
    判定は引数で差し替えられるようにし、テストが torch を読み込まずに済むようにする。
    """
    if configured != "auto":
        return configured
    if cuda_available():
        return "cuda"
    return "xpu" if xpu_available() else "cpu"


@lru_cache(maxsize=1)
def load_model(
    model_name: str, device: str, max_length: int, revision: str | None
) -> SentenceTransformer:
    """モデルを読み込む。プロセス内で 1 度だけ実行される。"""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - 依存が入っていれば起きない
        message = "sentence-transformers が利用できません"
        raise EmbeddingModelLoadError(message) from exc

    logger.info("Embedding モデルを読み込みます: %s (device=%s)", model_name, device)
    try:
        model = SentenceTransformer(
            model_name,
            # SentenceTransformer はデバイス省略時に cuda / mps / cpu しか
            # 自動選択せず、xpu は対象外（明示指定が必須）。ここで
            # resolve_device の結果を明示的に渡しているからこそ XPU で動く。
            # 将来 device 引数を省略する変更を入れると、XPU 環境では黙って
            # CPU へ落ちるため注意する。
            device=device,
            revision=revision,
            # モデルリポジトリ側のコードを実行しない。
            trust_remote_code=False,
        )
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

    @cached_property
    def device(self) -> str:
        """実際に使用するデバイス。

        最初に参照された時点で `resolve_device` を呼んで解決し、以降は
        `functools.cached_property` がインスタンスへキャッシュした値を返す。
        `__init__` で解決すると、ジョブハンドラの登録
        （`create_default_registry`）だけで `resolve_device` 経由の
        `import torch` が走ってしまうため、実際に必要になるまで遅延させる
        （Issue #80）。

        **同時アクセス時は `resolve_device` が複数回走りうる。** Python 3.12 の
        `cached_property` は排他制御を持たず（3.10 系にあった `RLock` は撤去
        された）、ワーカーは `worker_concurrency` ぶんのジョブをそれぞれ別スレッド
        （`asyncio.to_thread`）で処理する一方、`make_embed_article_handler` は
        プロバイダーを 1 個だけ作って使い回す。2 件の `embed_article` がほぼ同時に
        走ると、両方のスレッドが初回参照に入りうる。

        ロックは入れない。`resolve_device` は決定的で副作用が無く、内部の
        `import torch` も CPython のモジュール単位のロックで直列化されるため、
        どちらのスレッドが書いても同じ値になる。排他を足しても防げるのは
        「二度計算すること」だけで、それに見合わない。
        """
        return resolve_device(self._settings.embedding_device)

    def _model(self) -> SentenceTransformer:
        return load_model(
            self._settings.embedding_model,
            self.device,
            self._settings.embedding_max_length,
            self._settings.embedding_model_revision,
        )

    def _truncate(self, text: str) -> str:
        """トークナイズ前に本文を切る。

        `max_seq_length` はトークナイズ後に効くため、巨大な本文をそのまま
        渡すとトークナイズ自体で CPU とメモリを消費する。
        """
        limit = self._settings.embedding_max_input_characters
        return text[:limit]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文書を埋め込む。空の入力ではモデルを読み込まない。"""
        if not texts:
            return []

        vectors = self._model().encode(
            [self._truncate(text) for text in texts],
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
            [self._truncate(text)],
            batch_size=1,
            normalize_embeddings=True,
            show_progress_bar=False,
            prompt=f"Instruct: {QUERY_INSTRUCTION}\nQuery: ",
        )[0]
        result = [float(value) for value in vector]
        assert_dimensions([result])
        return result
