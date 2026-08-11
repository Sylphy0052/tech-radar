"""設定読み込みの振る舞いを検証する。"""

import pytest

from techradar.config import Settings, get_settings


def test_returns_default_embedding_dimensions_matching_pgvector_schema():
    # Arrange / Act
    settings = Settings(_env_file=None)

    # Assert — pgvector の vector(1024) と一致していること
    assert settings.embedding_dimensions == 1024


def test_treats_empty_brave_api_key_as_unset():
    # Arrange / Act
    settings = Settings(_env_file=None, brave_search_api_key="")

    # Assert
    assert settings.brave_search_api_key is None
    assert settings.is_brave_search_enabled is False


def test_enables_brave_search_when_api_key_is_present():
    # Arrange / Act
    settings = Settings(_env_file=None, brave_search_api_key="dummy-key")

    # Assert
    assert settings.is_brave_search_enabled is True


def test_allows_the_local_frontend_origin_by_default():
    # Arrange / Act
    settings = Settings(_env_file=None)

    # Assert — run.sh が既定で起動する frontend のオリジンと一致していること
    assert settings.cors_allow_origins == ["http://localhost:13700"]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://localhost:13700", ["http://localhost:13700"]),
        (
            "http://localhost:13700,http://127.0.0.1:13700",
            ["http://localhost:13700", "http://127.0.0.1:13700"],
        ),
        (
            " http://localhost:13700 , http://127.0.0.1:13700 ",
            ["http://localhost:13700", "http://127.0.0.1:13700"],
        ),
        ("http://localhost:13700,,", ["http://localhost:13700"]),
    ],
)
def test_splits_comma_separated_cors_allow_origins(configured: str, expected: list[str]):
    # Arrange / Act
    settings = Settings(_env_file=None, cors_allow_origins=configured)

    # Assert
    assert settings.cors_allow_origins == expected


@pytest.mark.parametrize(
    "configured",
    [
        "localhost:13700",  # スキームなし
        "http://localhost:13700/",  # 末尾スラッシュ付き（Origin ヘッダと文字列一致しない）
        "ftp://localhost:13700",  # http(s) 以外のスキーム
    ],
)
def test_rejects_malformed_cors_allow_origins(configured: str):
    # Arrange / Act / Assert — 設定ミスを preflight 失敗ではなく起動時に気付けるようにする
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_allow_origins=configured)


def test_rejects_empty_cors_allow_origins():
    # Arrange / Act / Assert — 全オリジン拒否の設定を事故で作らないため
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_allow_origins="")


# 上の一連のテストは初期化引数で値を渡している。この経路は pydantic-settings の
# 複合型デコードを通らないため、実際の設定ファイルや環境変数から読む経路の壊れ方を
# 検出できない（Issue #58 はそれで見逃された）。以降はその経路を通す。
#
# 設定ファイルと環境変数はデコードを担う実装が別（`DotEnvSettingsSource` と
# `EnvSettingsSource`）なので、同じ記法を両方へ通して食い違いが出ないようにする。
_CORS_ORIGIN_CASES = [
    # 配布しているサンプルと同じ、単一オリジンをそのまま書いた形
    ("http://localhost:13700", ["http://localhost:13700"]),
    (
        "http://localhost:13700,http://127.0.0.1:13700",
        ["http://localhost:13700", "http://127.0.0.1:13700"],
    ),
    # JSON 配列の記法。pydantic-settings が本来受け付ける形で、
    # 既にこの形で書かれている設定を壊さない
    ('["http://localhost:13700"]', ["http://localhost:13700"]),
    (
        '["http://localhost:13700", "http://127.0.0.1:13700"]',
        ["http://localhost:13700", "http://127.0.0.1:13700"],
    ),
]


@pytest.mark.parametrize(("configured", "expected"), _CORS_ORIGIN_CASES)
def test_reads_cors_allow_origins_from_a_settings_file(
    tmp_path, configured: str, expected: list[str]
):
    # Arrange
    settings_file = tmp_path / "settings"
    settings_file.write_text(f"CORS_ALLOW_ORIGINS={configured}\n", encoding="utf-8")

    # Act
    settings = Settings(_env_file=str(settings_file))

    # Assert
    assert settings.cors_allow_origins == expected


@pytest.mark.parametrize(("configured", "expected"), _CORS_ORIGIN_CASES)
def test_reads_cors_allow_origins_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: list[str]
):
    # Arrange
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", configured)

    # Act
    settings = Settings(_env_file=None)

    # Assert
    assert settings.cors_allow_origins == expected


def test_rejects_malformed_cors_allow_origins_from_a_settings_file(tmp_path):
    # Arrange — 設定ファイル経由でも表記の検証が効くこと
    settings_file = tmp_path / "settings"
    settings_file.write_text("CORS_ALLOW_ORIGINS=localhost:13700\n", encoding="utf-8")

    # Act / Assert
    with pytest.raises(ValueError):
        Settings(_env_file=str(settings_file))


def test_rejects_non_positive_worker_concurrency():
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        Settings(_env_file=None, worker_concurrency=0)


def test_caches_settings_singleton():
    # Arrange
    get_settings.cache_clear()

    # Act
    first = get_settings()
    second = get_settings()

    # Assert
    assert first is second


@pytest.mark.parametrize(
    "configured",
    [
        # ワイルドカード様の表記。現状の `CORSMiddleware` は完全一致比較のため
        # マッチしないが、`allow_origin_regex` を併用する変更が入った途端に
        # 実際のワイルドカードとして機能する（`allow_credentials=True` と組む）
        "http://*",
        "https://*.evil.com",
        "http://*.localhost:13700",
        # 大文字を含む表記。ブラウザの `Origin` ヘッダはスキームとホストが
        # 小文字に正規化されて届くため、設定しても一致せず preflight だけが落ちる
        "HTTP://LOCALHOST:13700",
        "http://LocalHost:13700",
        "HTTPS://example.com",
        # userinfo。`Origin` ヘッダには含まれないため一致しない
        "http://user:pass@localhost:13700",
        "http://user@localhost:13700",
        # ホスト名として成立しない
        "http://:13700",
    ],
)
def test_rejects_cors_allow_origins_that_cannot_match_an_origin_header(configured: str):
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        Settings(_env_file=None, cors_allow_origins=configured)


@pytest.mark.parametrize(
    "configured",
    [
        "http://localhost:13700",
        "https://example.com",
        "http://127.0.0.1:13700",
        "https://sub.example.co.jp",
        "http://[::1]:13700",
    ],
)
def test_accepts_cors_allow_origins_that_match_an_origin_header(configured: str):
    # Arrange / Act — 検証を厳しくしても正常系を巻き込まないこと
    settings = Settings(_env_file=None, cors_allow_origins=configured)

    # Assert
    assert settings.cors_allow_origins == [configured]
