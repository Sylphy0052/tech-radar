"""Embedding 実行環境の起動時検査を固定するテスト（Issue #78）。

2026-08-12、venv のインストールが不完全なまま起動し、`embed_article`
ジョブ 194 件が同じ理由で全滅した。この検査がなければ 194 件が失敗するまで
気付けなかった事象を再現する形で、import 失敗・デバイス利用不可・想定外の
例外のそれぞれが検知できることを固定する。

モデルの実ロードは行わない仕様のため、ここでは `torch` /
`sentence_transformers` を実際に import しない（`import_torch` /
`import_sentence_transformers` を差し替える）。`resolve_device` 自体は
torch に依存しないため実体を使い、`cuda_available` / `xpu_available` だけを
差し替える。
"""

from __future__ import annotations

import pytest

from techradar.embedding.health import EmbeddingHealthCheckResult, check_embedding_health


def _ok_import() -> None:
    """import に成功したことを表すダミー。"""


class TestCheckEmbeddingHealth:
    def test_成功時はデバイスが返る(self) -> None:
        """import・デバイス判定のすべてが通れば ok=True でデバイスが返ることを固定する。"""
        # Arrange / Act
        result = check_embedding_health(
            "auto",
            import_torch=_ok_import,
            import_sentence_transformers=_ok_import,
            cuda_available=lambda: False,
            xpu_available=lambda: True,
        )

        # Assert
        assert result == EmbeddingHealthCheckResult(ok=True, device="xpu")

    def test_torchをimportできない場合は失敗として返る(self) -> None:
        """`ModuleNotFoundError` を送出せず、結果として返すことを固定する。

        実際の事象（Issue #78）では `torch` の import 自体が壊れており、
        DB には `sentence-transformers が利用できません` という 1 行しか
        残らず、原因が読み取れなかった。この検査は `torch` の import 失敗を
        `sentence_transformers` とは別の段階として検知し、例外の型と
        メッセージをそのまま結果へ残す。
        """

        # Arrange
        def _raise_torch_import_error() -> None:
            message = "No module named 'torch'"
            raise ModuleNotFoundError(message)

        # Act
        result = check_embedding_health(
            "auto",
            import_torch=_raise_torch_import_error,
            import_sentence_transformers=_ok_import,
            cuda_available=lambda: False,
            xpu_available=lambda: False,
        )

        # Assert
        assert result.ok is False
        assert result.device is None
        assert result.error_type == "ModuleNotFoundError"
        assert result.error_message == "No module named 'torch'"

    def test_sentence_transformersをimportできない場合は失敗として返る(self) -> None:
        """Issue #78 の実事象そのもの（DB に残った 1 行）を固定する。

        `torch` の import は通るが `sentence_transformers` が壊れている場合も、
        `torch` の場合と区別できる形で結果に残ることを確かめる。
        """

        # Arrange
        def _raise_sentence_transformers_import_error() -> None:
            message = "Could not import module 'PreTrainedModel'"
            raise ModuleNotFoundError(message)

        # Act
        result = check_embedding_health(
            "auto",
            import_torch=_ok_import,
            import_sentence_transformers=_raise_sentence_transformers_import_error,
            cuda_available=lambda: False,
            xpu_available=lambda: False,
        )

        # Assert
        assert result.ok is False
        assert result.device is None
        assert result.error_type == "ModuleNotFoundError"
        assert "PreTrainedModel" in (result.error_message or "")

    def test_明示指定したデバイスが実際には使えない場合は失敗として返る(self) -> None:
        """`resolve_device` は `auto` 以外では可用性を確認せずそのまま返す。

        `xpu` を明示指定したのに `torch.xpu.is_available()` が False という、
        Issue #78 の想定シナリオそのものを固定する。
        """
        # Arrange / Act
        result = check_embedding_health(
            "xpu",
            import_torch=_ok_import,
            import_sentence_transformers=_ok_import,
            cuda_available=lambda: False,
            xpu_available=lambda: False,
        )

        # Assert
        assert result.ok is False
        assert result.device == "xpu"
        assert result.error_type is not None
        assert "xpu" in (result.error_message or "")

    def test_cpuは常に使える扱いになる(self) -> None:
        """`cpu` はデバイス判定用の可用性チェックを介さず常に通る。"""
        # Arrange / Act
        result = check_embedding_health(
            "cpu",
            import_torch=_ok_import,
            import_sentence_transformers=_ok_import,
            cuda_available=lambda: (_ for _ in ()).throw(AssertionError("呼ばれないはず")),
            xpu_available=lambda: (_ for _ in ()).throw(AssertionError("呼ばれないはず")),
        )

        # Assert
        assert result == EmbeddingHealthCheckResult(ok=True, device="cpu")

    def test_想定外の例外を送出しても結果として返る(self) -> None:
        """検査は補助であり、判定関数がどんな例外を投げても呼び出し側を止めない。

        `main.lifespan` は例外送出そのものにも耐えるが、`check_embedding_health`
        自身も可能な限り例外を握り潰して結果として返すことを固定する。
        """

        # Arrange
        def _boom(*_args: object, **_kwargs: object) -> str:
            message = "unexpected failure"
            raise RuntimeError(message)

        # Act
        result = check_embedding_health(
            "auto",
            import_torch=_ok_import,
            import_sentence_transformers=_ok_import,
            resolve_device_fn=_boom,
        )

        # Assert
        assert result.ok is False
        assert result.device is None
        assert result.error_type == "RuntimeError"
        assert result.error_message == "unexpected failure"

    @pytest.mark.parametrize("configured", ["auto", "cpu", "cuda", "xpu"])
    def test_結果はfrozenである(self, configured: str) -> None:
        """`dataclass(frozen=True)` で不変であることを固定する（規約: coding-style）。"""
        # Arrange
        result = check_embedding_health(
            configured,
            import_torch=_ok_import,
            import_sentence_transformers=_ok_import,
            cuda_available=lambda: True,
            xpu_available=lambda: True,
        )

        # Act / Assert — `setattr` 経由にすることで静的型チェッカーの検査対象から外す
        # （直接代入だと `ty` が読み取り専用プロパティへの代入として検出してしまう）
        with pytest.raises(AttributeError):
            setattr(result, "ok", not result.ok)  # noqa: B010
