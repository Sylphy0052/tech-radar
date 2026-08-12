"""アプリケーションのヘルスチェックと lifespan（ジョブワーカー起動）を検証する。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from techradar import __version__
from techradar import main as main_module
from techradar.config import Settings
from techradar.embedding.health import EmbeddingHealthCheckResult
from techradar.jobs.registry import JobHandlerRegistry
from techradar.main import create_app, lifespan


def test_health_returns_ok_with_version():
    # Arrange
    app = create_app(Settings(_env_file=None))
    client = TestClient(app)

    # Act
    response = client.get("/api/health")

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "brave_search_enabled": False,
    }


def test_health_reports_brave_search_enabled_when_key_configured():
    # Arrange
    app = create_app(Settings(_env_file=None, brave_search_api_key="dummy-key"))
    client = TestClient(app)

    # Act
    response = client.get("/api/health")

    # Assert
    assert response.json()["brave_search_enabled"] is True


def test_keeps_injected_settings_after_lifespan_startup():
    # Arrange — TestClient を context manager として使うと lifespan が実行される
    app = create_app(Settings(_env_file=None, brave_search_api_key="dummy-key"))

    # Act
    with TestClient(app) as client:
        response = client.get("/api/health")

    # Assert — 起動処理が注入した設定を上書きしないこと
    assert response.json()["brave_search_enabled"] is True


def test_allows_cors_preflight_from_a_configured_origin():
    # Arrange
    app = create_app(Settings(_env_file=None, cors_allow_origins="http://localhost:19999"))
    client = TestClient(app)

    # Act
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:19999",
            "Access-Control-Request-Method": "GET",
        },
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:19999"


def test_rejects_cors_preflight_from_an_unconfigured_origin():
    # Arrange
    app = create_app(Settings(_env_file=None, cors_allow_origins="http://localhost:19999"))
    client = TestClient(app)

    # Act
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )

    # Assert — 許可していないオリジンには Allow-Origin を返さないこと
    assert "access-control-allow-origin" not in response.headers


def test_does_not_start_the_job_worker_when_worker_enabled_is_false():
    # Arrange — 実ワーカーが DB をポーリングし始めるとテストが不安定になるため、
    # 無効化した場合に lifespan を通してもワーカーが起動しないことを確認する。
    app = create_app(Settings(_env_file=None, worker_enabled=False))

    # Act
    with TestClient(app):
        pass

    # Assert
    assert app.state.job_worker is None


def test_does_not_call_embedding_health_check_when_worker_enabled_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — self review（HIGH）対応。`_check_embedding_health` の呼び出しは
    # `if settings.worker_enabled:` の内側にある。この配置そのものを直接固定
    # しないと、将来この呼び出しがブロックの外へ動いてもテストは気付けない。
    # torch / sentence_transformers のコールド import は重く、
    # `WORKER_ENABLED=false` を既定とするテスト群（backend の大半）が
    # 一斉に遅くなる回帰を防ぐのが狙い。呼び出し関数そのものを spy 化して
    # 呼び出し回数を直接検証する。
    calls: list[tuple[Any, ...]] = []

    def _spy(*args: object, **kwargs: object) -> EmbeddingHealthCheckResult:
        calls.append(args)
        return EmbeddingHealthCheckResult(ok=True, device="cpu")

    monkeypatch.setattr(main_module, "check_embedding_health", _spy)
    app = create_app(Settings(_env_file=None, worker_enabled=False))

    # Act
    with TestClient(app):
        pass

    # Assert — 検査関数そのものが一度も呼ばれていないこと
    assert len(calls) == 0


class _StubJobWorker:
    """`JobWorker` の代わりに使う、DB へ触れないスタブ。

    実ワーカーは DB をポーリングするため、`start`/`stop` が呼ばれたことだけを
    記録し、テストがタイムアウトしたり DB と干渉したりしないようにする。
    """

    instances: ClassVar[list[_StubJobWorker]] = []

    def __init__(self, *, settings: Settings, registry: JobHandlerRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.started = False
        self.stopped = False
        _StubJobWorker.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _create_app_with_stub_worker(monkeypatch: pytest.MonkeyPatch, **settings_kwargs: Any) -> Any:
    """`JobWorker` をスタブへ差し替えた状態で `worker_enabled=True` のアプリを作る。

    `TestEmbeddingHealthCheckInLifespan` のテストと、lifespan を直接呼ぶ
    非同期テストの両方から使う共通ヘルパー。
    """
    _StubJobWorker.instances = []
    monkeypatch.setattr("techradar.main.JobWorker", _StubJobWorker)
    return create_app(Settings(_env_file=None, worker_enabled=True, **settings_kwargs))


async def _await_embedding_health_check_task(app: Any) -> None:
    """`lifespan` が `app.state` に積んだ検査タスクの完了を待つ。

    検査は `lifespan` の中で `asyncio.create_task` により起動処理から切り離される
    ため、`TestClient` の `with` ブロックを抜けるだけでは完了を保証できない
    （タイミングによってはシャットダウン時にキャンセルされ、ログが出ないまま
    終わる）。`TestClient` は ASGI アプリを別スレッドのイベントループ上で回すため、
    ログの有無をアサートするテストでは `client.portal.call` 経由でこの関数を
    そのループ上で実行し、確実に完了を待ってから検証する。
    """
    task = app.state.embedding_health_check_task
    assert task is not None
    await task


def _wait_for_embedding_health_check(client: TestClient, app: Any) -> None:
    """`client.portal` 経由で検査タスクの完了を待つ薄いラッパー。

    `TestClient.portal` は `with TestClient(app) as client:` の内側でのみ
    設定される（型は `BlockingPortal | None`）ため、ここで存在を確認してから使う。
    """
    portal = client.portal
    assert portal is not None
    portal.call(_await_embedding_health_check_task, app)


def test_starts_and_stops_the_job_worker_when_worker_enabled_is_true(
    monkeypatch: pytest.MonkeyPatch,
):
    # Arrange — Embedding 実行環境の検査（Issue #78）は worker_enabled と同じ場所で
    # 実行される。実物の torch / sentence_transformers を読み込むと初回 import だけで
    # 数十秒かかる（実測）ため、このテストの本題（ワーカーの起動・停止）とは無関係な
    # コストを避けてスタブへ差し替える。
    _StubJobWorker.instances = []
    monkeypatch.setattr("techradar.main.JobWorker", _StubJobWorker)
    monkeypatch.setattr(
        main_module,
        "check_embedding_health",
        lambda *_a, **_k: EmbeddingHealthCheckResult(ok=True, device="cpu"),
    )
    app = create_app(Settings(_env_file=None, worker_enabled=True))

    # Act
    with TestClient(app):
        # Assert — 起動処理の中で start() 済みであること
        assert len(_StubJobWorker.instances) == 1
        worker: Any = _StubJobWorker.instances[0]
        assert worker.started is True
        assert worker.stopped is False

    # Assert — コンテキスト終了（lifespan のシャットダウン）で stop() 済みであること
    assert worker.stopped is True


async def test_lifespan_startup_does_not_wait_for_the_embedding_health_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修正1（self review、最優先）の主眼を直接固定する。

    検査には実測 8.3〜20.3 秒かかる（`embedding/health.py` docstring 参照）。
    起動処理（`lifespan` の `yield` の手前）がこれを待たずに戻ることを、
    スレッドイベントで検査関数をブロックさせたうえで直接確認する。
    """
    # Arrange
    started = threading.Event()
    release = threading.Event()

    def _blocking_check(*_args: object, **_kwargs: object) -> EmbeddingHealthCheckResult:
        started.set()
        # release が来なければテスト設定側の不備。5秒待っても来なければ諦める。
        assert release.wait(timeout=5), "release イベントが来なかった（テスト側の不備）"
        return EmbeddingHealthCheckResult(ok=True, device="cpu")

    monkeypatch.setattr(main_module, "check_embedding_health", _blocking_check)
    app = _create_app_with_stub_worker(monkeypatch)

    # Act — lifespan の起動処理（yield の手前まで）を直接実行する
    async with lifespan(app):
        # Assert — 検査はまだブロック中だが、起動処理はここまで先に進んでいる。
        # `threading.Event.wait` はイベントループを止める素朴な待ち方のため、
        # `asyncio.sleep(0)` を挟んで明示的にループへ制御を戻しながらポーリング
        # する（回数で上限を設け、壁時計そのものには依存しない）。
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "検査タスクが開始されなかった"
        task = app.state.embedding_health_check_task
        assert task is not None
        assert not task.done()

        # Cleanup — ブロックを解除して検査を完了させる（finally の後始末が
        # キャンセル経路ではなく正常完了経路を通ることを確認するため、
        # ここで明示的に待ち切ってから with ブロックを抜ける）
        release.set()
        await task
        assert task.done()


async def test_lifespan_shutdown_cancels_the_embedding_health_check_task_when_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """終了時にまだ検査が走っていた場合の後始末を固定する（self review 最優先）。

    検査本体は asyncio.to_thread で実スレッドへ逃がしてあるため、Task.cancel()
    はそのスレッドを止められない（Python のスレッドは強制終了できない）。
    それでも `lifespan` の終了処理（finally）自体は、検査の完了を待ち切らず
    戻ることを確認する。15秒の上限は厳密な処理時間のアサーションではなく、
    「無期限にはハングしない」ことを確かめるための緩い上限（このリポジトリの
    既存の同種テスト `test_stop_does_not_hang_when_a_handler_swallows_cancelled_error`
    の `_WAIT_TIMEOUT_SECONDS = 15.0` に合わせた）。
    """
    # Arrange
    started = threading.Event()
    release = threading.Event()

    def _blocking_check(*_args: object, **_kwargs: object) -> EmbeddingHealthCheckResult:
        started.set()
        # このテストではあえて release.set() を呼ばない（呼ばれない場合は
        # このスレッド自身が5秒で諦めて戻るだけで、結果を待つ者はいない）。
        release.wait(timeout=5)
        return EmbeddingHealthCheckResult(ok=True, device="cpu")

    monkeypatch.setattr(main_module, "check_embedding_health", _blocking_check)
    app = _create_app_with_stub_worker(monkeypatch)
    task_holder: list[asyncio.Task[None] | None] = [None]

    async def _enter_and_exit_while_check_is_still_running() -> None:
        async with lifespan(app):
            for _ in range(500):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set(), "検査タスクが開始されなかった"
            task = app.state.embedding_health_check_task
            assert task is not None
            assert not task.done()
            task_holder[0] = task
            # release を呼ばずに with ブロックを抜ける
            # → finally が cancel 経路を通ることを確認する

    try:
        # Act — 終了処理がハングせずに戻ってくること
        await asyncio.wait_for(_enter_and_exit_while_check_is_still_running(), timeout=15.0)
    finally:
        # Cleanup — 取り残されたスレッドを完走させ、後続のテストへ影響を残さない
        release.set()

    # Assert — finally が cancel() を呼び、Task がキャンセル済みで終わっていること
    task = task_holder[0]
    assert task is not None
    assert task.cancelled()


class TestEmbeddingHealthCheckInLifespan:
    """起動時の Embedding 実行環境検査を固定する（Issue #78）。

    2026-08-12、venv のインストールが不完全なまま起動し `embed_article`
    ジョブ 194 件が全滅した。検査に失敗しても、また検査関数自体が想定外の
    例外を投げても、記事登録やフィード表示に使うアプリの起動は続くことを
    固定する。ログの検証は `caplog` ではなく `techradar.main.logger` を直接
    差し替える。並列実行のワーカーでは `caplog` がハンドラを拾えず、実装が
    正しくても落ちることがある（`test_llm_managed_policy.py` / `test_jobs_worker.py`
    と同じ理由）。

    self review（最優先）対応で検査は `asyncio.create_task` により起動処理から
    切り離された（`test_lifespan_startup_does_not_wait_for_the_embedding_health_check`
    参照）。そのため `TestClient` の `with` ブロックを抜けただけでは検査が
    完了している保証が無く、ログの有無を見るテストは
    `_await_embedding_health_check_task` で完了を待ってから検証する。
    """

    def _create_app_with_stub_worker(
        self, monkeypatch: pytest.MonkeyPatch, **settings_kwargs: Any
    ) -> Any:
        return _create_app_with_stub_worker(monkeypatch, **settings_kwargs)

    def test_検査に失敗しても起動が続く(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            main_module,
            "check_embedding_health",
            lambda *_a, **_k: EmbeddingHealthCheckResult(
                ok=False,
                error_type="ModuleNotFoundError",
                error_message="No module named 'torch'",
            ),
        )
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act
        with TestClient(app) as client:
            response = client.get("/api/health")
            _wait_for_embedding_health_check(client, app)

        # Assert — 検査が失敗してもヘルスチェックには応答し続ける
        assert response.status_code == 200

    def test_検査関数が想定外の例外を投げても起動が続く(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        def _raise(*_args: object, **_kwargs: object) -> EmbeddingHealthCheckResult:
            message = "unexpected failure"
            raise RuntimeError(message)

        monkeypatch.setattr(main_module, "check_embedding_health", _raise)
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act
        with TestClient(app) as client:
            response = client.get("/api/health")
            _wait_for_embedding_health_check(client, app)

        # Assert
        assert response.status_code == 200

    def test_成功時はデバイスを含むINFOログが出る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            main_module,
            "check_embedding_health",
            lambda *_a, **_k: EmbeddingHealthCheckResult(ok=True, device="xpu"),
        )
        info_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            main_module.logger, "info", lambda *args, **_kwargs: info_calls.append(args)
        )
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act — 背景実行のため、ログを検証する前に検査タスクの完了を待つ
        with TestClient(app) as client:
            _wait_for_embedding_health_check(client, app)

        # Assert
        assert any("xpu" in call for call in info_calls)

    def test_失敗時は原因を含むERRORログが出る(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.setattr(
            main_module,
            "check_embedding_health",
            lambda *_a, **_k: EmbeddingHealthCheckResult(
                ok=False,
                error_type="ModuleNotFoundError",
                error_message="No module named 'torch'",
            ),
        )
        error_calls: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            main_module.logger, "error", lambda *args, **_kwargs: error_calls.append(args)
        )
        app = self._create_app_with_stub_worker(monkeypatch)

        # Act — 背景実行のため、ログを検証する前に検査タスクの完了を待つ
        with TestClient(app) as client:
            _wait_for_embedding_health_check(client, app)

        # Assert — 例外の型とメッセージが読み取れること
        assert any(
            "ModuleNotFoundError" in call and "No module named 'torch'" in call
            for call in error_calls
        )
