"""Embedding 実行環境の起動時検査（Issue #78）。

2026-08-12、venv のインストールが不完全な状態のまま起動し、`embed_article`
ジョブ 194 件が同じ理由（`sentence-transformers が利用できません`）で
全滅した。実際の原因は `torch` の import 自体が壊れていることだったが、
194 件が失敗するまで気付けなかった。

この検査は起動時に import とデバイス判定だけを行い、モデルの実ロードは
行わない（実ロードは実測で 8〜16 秒かかり、起動を遅くする。Issue #77）。
判定に使う関数を引数で差し替えられるようにし、テストが `torch` を
実際に読み込まずに済むようにする（`embedding.qwen` の
`is_cuda_available` / `is_xpu_available` と同じ流儀）。

`techradar.llm.managed_policy` と違い、この検査はフェイルオープンである。
Embedding が動かなくても記事登録やフィード表示は成立するため、検査の
失敗を理由に起動を止めると無関係な機能まで使えなくなる。呼び出し側
（`main.lifespan`）は検査結果に応じてログを出すだけで、起動は続ける。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from techradar.embedding.qwen import is_cuda_available, is_xpu_available, resolve_device


@dataclass(frozen=True)
class EmbeddingHealthCheckResult:
    """Embedding 実行環境検査の結果。

    `ok` が偽のとき、`error_type` / `error_message` に失敗の詳細が入る。
    `device` は検査が `resolve_device` まで到達できた場合のみ入る
    （import 段階で失敗した場合は None のまま）。
    """

    ok: bool
    device: str | None = None
    error_type: str | None = None
    error_message: str | None = None


def _import_torch() -> None:
    """`torch` を import できるかだけを確かめる。"""
    import torch  # noqa: F401


def _import_sentence_transformers() -> None:
    """`sentence_transformers` を import できるかだけを確かめる。"""
    import sentence_transformers  # noqa: F401


def _device_is_usable(
    device: str,
    *,
    cuda_available: Callable[[], bool],
    xpu_available: Callable[[], bool],
) -> bool:
    """`resolve_device` が選んだデバイスが実際に使えるかを確かめる。

    `cpu` は常に使える。`cuda` / `xpu` はそれぞれの `is_available()` 相当の
    判定を通す。`configured` が明示指定（`auto` 以外）のときは `resolve_device`
    が可用性を確認せずそのまま返すため、ここで改めて確認する意味がある。
    """
    if device == "cuda":
        return cuda_available()
    if device == "xpu":
        return xpu_available()
    return True


def check_embedding_health(
    configured_device: str,
    *,
    import_torch: Callable[[], None] = _import_torch,
    import_sentence_transformers: Callable[[], None] = _import_sentence_transformers,
    resolve_device_fn: Callable[..., str] = resolve_device,
    cuda_available: Callable[[], bool] = is_cuda_available,
    xpu_available: Callable[[], bool] = is_xpu_available,
) -> EmbeddingHealthCheckResult:
    """Embedding の実行環境を検査する。モデルの実ロードは行わない。

    次を順に確かめる。
    1. `torch` を import できる
    2. `sentence_transformers` を import できる
    3. `resolve_device` が返すデバイスが実際に使える

    想定外の例外を含め、失敗はすべて `EmbeddingHealthCheckResult(ok=False, ...)`
    として返す（例外を送出しない）。検査は補助であり、これを呼ぶ側
    （`main.lifespan`）の起動処理を止めないため。
    """
    try:
        import_torch()
        import_sentence_transformers()
        device = resolve_device_fn(
            configured_device, cuda_available=cuda_available, xpu_available=xpu_available
        )
        if not _device_is_usable(
            device, cuda_available=cuda_available, xpu_available=xpu_available
        ):
            message = f"resolve_device が選択したデバイス '{device}' が実際には使用できません"
            return EmbeddingHealthCheckResult(
                ok=False,
                device=device,
                error_type="EmbeddingDeviceUnavailable",
                error_message=message,
            )
    except Exception as exc:
        return EmbeddingHealthCheckResult(
            ok=False, error_type=type(exc).__name__, error_message=str(exc)
        )

    return EmbeddingHealthCheckResult(ok=True, device=device)
