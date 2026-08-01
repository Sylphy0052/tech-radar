"""Qwen3-Embedding プロバイダーを検証する。

実モデルの読み込みは 1.2GB のダウンロードと数秒〜数十秒を要するため、
既定では実行しない。`TECHRADAR_RUN_MODEL_TESTS=1` を付けたときだけ動く。

モデルを使わない部分（デバイス選択・次元検証・設定の反映）は常に検証する。
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import pytest

from techradar.config import Settings
from techradar.db import EMBEDDING_DIMENSIONS
from techradar.embedding.base import assert_dimensions
from techradar.embedding.errors import EmbeddingDimensionMismatchError
from techradar.embedding.qwen import QwenEmbeddingProvider, resolve_device

requires_model = pytest.mark.skipif(
    os.environ.get("TECHRADAR_RUN_MODEL_TESTS") != "1",
    reason="実モデルを読み込むテスト。TECHRADAR_RUN_MODEL_TESTS=1 で実行する",
)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """正規化済みベクトル同士のコサイン類似度。"""
    return sum(a * b for a, b in zip(left, right, strict=True))


class TestResolveDevice:
    @pytest.mark.parametrize("configured", ["cpu", "cuda"])
    def test_respects_an_explicit_device(self, configured: str):
        # Arrange / Act / Assert — 明示指定はそのまま使う
        assert resolve_device(configured) == configured

    def test_auto_falls_back_to_cpu_without_cuda(self):
        # Arrange / Act / Assert — 判定を注入し、torch を読み込まずに検証する
        assert resolve_device("auto", cuda_available=lambda: False) == "cpu"

    def test_auto_selects_cuda_when_available(self):
        # Arrange / Act / Assert
        assert resolve_device("auto", cuda_available=lambda: True) == "cuda"


class TestDimensionGuard:
    def test_accepts_vectors_of_the_expected_dimension(self):
        # Arrange / Act / Assert
        assert_dimensions([[0.0] * EMBEDDING_DIMENSIONS])

    def test_rejects_vectors_of_a_different_dimension(self):
        # Arrange — モデルを差し替えた際の取り違えを保存より手前で検出する
        with pytest.raises(EmbeddingDimensionMismatchError, match="次元が一致しません"):
            assert_dimensions([[0.0] * 512])

    def test_checks_every_vector(self):
        # Arrange / Act / Assert
        with pytest.raises(EmbeddingDimensionMismatchError):
            assert_dimensions([[0.0] * EMBEDDING_DIMENSIONS, [0.0] * 384])


class TestProviderConfiguration:
    def test_reports_the_configured_dimension(self):
        # Arrange / Act
        provider = QwenEmbeddingProvider(Settings(_env_file=None))

        # Assert
        assert provider.dimensions == EMBEDDING_DIMENSIONS

    def test_uses_the_configured_device(self):
        # Arrange / Act
        provider = QwenEmbeddingProvider(Settings(_env_file=None, embedding_device="cpu"))

        # Assert
        assert provider.device == "cpu"

    def test_does_not_load_the_model_for_empty_input(self):
        # Arrange — 空入力でモデルを読み込むと無駄に数秒かかる
        provider = QwenEmbeddingProvider(Settings(_env_file=None, embedding_device="cpu"))

        # Act / Assert — 読み込みが起きればここで時間がかかるか例外になる
        assert provider.embed_documents([]) == []


@requires_model
class TestRealModel:
    """実モデルを読み込む検証。既定では実行しない。"""

    @pytest.fixture(scope="class")
    def provider(self) -> QwenEmbeddingProvider:
        return QwenEmbeddingProvider(Settings(_env_file=None))

    def test_produces_vectors_of_the_configured_dimension(self, provider):
        # Arrange / Act
        vectors = provider.embed_documents(["Model Context Protocol とは何か"])

        # Assert
        assert len(vectors) == 1
        assert len(vectors[0]) == EMBEDDING_DIMENSIONS

    def test_is_crosslingual(self, provider):
        # Arrange — 言語を限定しない要件のため、日英で同じ内容が近くなること
        japanese = "Model Context Protocol は LLM を外部ツールへ接続する標準規格です。"
        english = "Model Context Protocol is a standard for connecting LLMs to external tools."
        unrelated = "本日の東京の天気は晴れ、最高気温は 28 度の見込みです。"

        # Act
        vectors = provider.embed_documents([japanese, english, unrelated])

        # Assert — 同一内容の日英ペアが、無関係なペアより明確に近い
        same_meaning = cosine(vectors[0], vectors[1])
        different_meaning = cosine(vectors[0], vectors[2])
        assert same_meaning > different_meaning
        assert same_meaning - different_meaning > 0.1

    def test_query_and_document_of_the_same_topic_are_close(self, provider):
        # Arrange
        document = provider.embed_documents(
            ["pgvector は PostgreSQL でベクトル検索を行うための拡張です。"]
        )[0]
        related = provider.embed_query("PostgreSQL でベクトル検索をするには")
        unrelated = provider.embed_query("パスタの茹で時間")

        # Act / Assert
        assert cosine(document, related) > cosine(document, unrelated)

    def test_fits_within_the_gpu_memory_budget(self, provider):
        # Arrange
        import torch

        if provider.device != "cuda":
            pytest.skip("CUDA が使えない環境")
        torch.cuda.reset_peak_memory_stats()

        # Act — 記事相当の長さを複数件まとめて処理する
        provider.embed_documents(["技術記事の本文です。" * 200] * 4)

        # Assert — RTX 4050 の 6GB に収まること
        peak_gib = torch.cuda.max_memory_allocated() / 1024**3
        assert peak_gib < 5.0, f"VRAM 使用量が想定を超えました: {peak_gib:.2f} GiB"
