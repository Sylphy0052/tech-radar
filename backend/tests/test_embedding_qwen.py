"""Qwen3-Embedding プロバイダーを検証する。

実モデルの読み込みは 1.2GB のダウンロードと数秒〜数十秒を要するため、
既定では実行しない。`TECHRADAR_RUN_MODEL_TESTS=1` を付けたときだけ動く。

モデルを使わない部分（デバイス選択・次元検証・設定の反映）は常に検証する。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

import pytest

from techradar.config import EmbeddingDevice, Settings
from techradar.db import EMBEDDING_DIMENSIONS
from techradar.embedding.base import assert_dimensions
from techradar.embedding.errors import EmbeddingDimensionMismatchError
from techradar.embedding.qwen import QwenEmbeddingProvider, is_xpu_available, resolve_device

requires_model = pytest.mark.skipif(
    os.environ.get("TECHRADAR_RUN_MODEL_TESTS") != "1",
    reason="実モデルを読み込むテスト。TECHRADAR_RUN_MODEL_TESTS=1 で実行する",
)

# CUDA (専用 VRAM) の上限。RTX 4050 の 6GB に対し 1GB の余裕を見ている。
_CUDA_MEMORY_BUDGET_GIB = 5.0

# XPU (統合GPU、メインメモリ共有) の上限。CUDA の専用 VRAM とは前提が異なり、
# 固定の VRAM 容量に対する余裕ではなく「バッチサイズが暴走してホスト側の
# メインメモリと食い合っていないか」を検知する目的の閾値（ADR 0005）。
# 実測値は 4 件のバッチで 1.75GiB（Core Ultra 7 165H 統合GPU、2026-08-12）。
# 桁で余裕を持たせつつ異常な増加は検知できる水準として 4.0 を置く。
_XPU_MEMORY_BUDGET_GIB = 4.0


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """正規化済みベクトル同士のコサイン類似度。"""
    return sum(a * b for a, b in zip(left, right, strict=True))


class TestResolveDevice:
    @pytest.mark.parametrize("configured", ["cpu", "cuda", "xpu"])
    def test_respects_an_explicit_device(self, configured: str):
        # Arrange / Act / Assert — 明示指定はそのまま使う（xpu もそのまま通る）
        assert resolve_device(configured) == configured

    def test_auto_falls_back_to_cpu_without_cuda_or_xpu(self):
        # Arrange / Act / Assert — 判定を注入し、torch を読み込まずに検証する
        assert (
            resolve_device("auto", cuda_available=lambda: False, xpu_available=lambda: False)
            == "cpu"
        )

    def test_auto_selects_xpu_when_cuda_is_unavailable(self):
        # Arrange — 開発機は NVIDIA GPU を持たず Intel Arc（XPU）のみを持つ想定
        # Act / Assert
        assert (
            resolve_device("auto", cuda_available=lambda: False, xpu_available=lambda: True)
            == "xpu"
        )

    def test_auto_prefers_cuda_over_xpu_when_both_are_available(self):
        # Arrange — NVIDIA の dGPU がある環境では統合GPU（XPU）より高速なため
        # CUDA を優先することを固定する
        # Act / Assert
        assert (
            resolve_device("auto", cuda_available=lambda: True, xpu_available=lambda: True)
            == "cuda"
        )

    def test_auto_selects_cuda_when_available(self):
        # Arrange / Act / Assert — xpu 判定が呼ばれない（cuda が先に決着する）ことも兼ねて確認
        assert (
            resolve_device(
                "auto",
                cuda_available=lambda: True,
                xpu_available=lambda: (_ for _ in ()).throw(AssertionError("呼ばれないはず")),
            )
            == "cuda"
        )


class TestIsXpuAvailable:
    def test_returns_false_without_raising_when_torch_has_no_xpu_attribute(self, monkeypatch):
        # Arrange — 古い torch や XPU ビルドでない torch には `torch.xpu` が無い。
        # 属性が無いことを理由に例外を投げず False を返すことを固定する。
        import types

        fake_torch = types.SimpleNamespace()  # xpu 属性を持たないダミー
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        # Act / Assert
        assert is_xpu_available() is False

    def test_returns_true_when_torch_reports_xpu_available(self, monkeypatch):
        # Arrange — torch.xpu が存在し is_available() が True を返す場合
        import types

        fake_torch = types.SimpleNamespace(xpu=types.SimpleNamespace(is_available=lambda: True))
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        # Act / Assert
        assert is_xpu_available() is True

    def test_returns_false_when_torch_reports_xpu_unavailable(self, monkeypatch):
        # Arrange — torch.xpu は存在するが is_available() が False を返す場合
        import types

        fake_torch = types.SimpleNamespace(xpu=types.SimpleNamespace(is_available=lambda: False))
        monkeypatch.setitem(sys.modules, "torch", fake_torch)

        # Act / Assert
        assert is_xpu_available() is False


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

    @pytest.mark.parametrize("configured", ["cpu", "cuda", "xpu"])
    def test_uses_the_configured_device(self, configured: EmbeddingDevice):
        # Arrange / Act — xpu の明示指定（本 Issue の主目的）も含めて固定する
        provider = QwenEmbeddingProvider(Settings(_env_file=None, embedding_device=configured))

        # Assert
        assert provider.device == configured

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
        # Arrange — CUDA (専用 VRAM) と XPU (統合GPU、メインメモリ共有) の
        # 両方でメモリ計測 API が使える（torch.xpu.max_memory_allocated 等は
        # PyTorch 2.5 以降の XPU ビルドに存在する。ADR 0005）。
        import torch

        if provider.device == "cuda":
            memory_api = torch.cuda
            budget_gib = _CUDA_MEMORY_BUDGET_GIB
        elif provider.device == "xpu":
            memory_api = torch.xpu
            budget_gib = _XPU_MEMORY_BUDGET_GIB
        else:
            pytest.skip("CUDA/XPU が使えない環境")
        memory_api.reset_peak_memory_stats()

        # Act — 記事相当の長さを複数件まとめて処理する
        provider.embed_documents(["技術記事の本文です。" * 200] * 4)

        # Assert
        peak_gib = memory_api.max_memory_allocated() / 1024**3
        assert peak_gib < budget_gib, f"GPU メモリ使用量が想定を超えました: {peak_gib:.2f} GiB"
