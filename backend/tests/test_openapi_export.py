"""OpenAPI スキーマ書き出し（`PROJECT_SPEC.md` §24 型安全性）を検証する。

DB や外部サービスを必要としないことがこの機能の要件のため、
どのテストも `db_session` フィクスチャ（PostgreSQL 接続）を使わない。
"""

from __future__ import annotations

import json
from pathlib import Path

from techradar.openapi_export import build_openapi_schema, main, render_openapi_schema


def test_build_openapi_schema_contains_known_paths():
    # Arrange / Act
    schema = build_openapi_schema()

    # Assert — 実装済みエンドポイントがスキーマに含まれること
    assert "/api/health" in schema["paths"]
    assert "/api/articles" in schema["paths"]
    assert "/api/crawl/runs" in schema["paths"]
    assert "/api/jobs/{job_id}" in schema["paths"]


def test_build_openapi_schema_is_deterministic():
    # Arrange / Act — 2 回生成しても同一内容であること
    first = build_openapi_schema()
    second = build_openapi_schema()

    # Assert
    assert first == second
    assert render_openapi_schema(first) == render_openapi_schema(second)


def test_render_openapi_schema_sorts_keys_and_ends_with_newline():
    # Arrange
    schema = {"b": 1, "a": 2}

    # Act
    rendered = render_openapi_schema(schema)

    # Assert — キー順序を固定し、差分が出ないよう改行で終端する
    assert rendered == json.dumps({"a": 2, "b": 1}, indent=2, sort_keys=True) + "\n"


def test_main_writes_schema_to_the_given_path(tmp_path: Path):
    # Arrange
    output_path = tmp_path / "openapi.json"

    # Act
    exit_code = main([str(output_path)])

    # Assert
    assert exit_code == 0
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert "/api/health" in written["paths"]


def test_main_defaults_to_the_repository_openapi_json_path(monkeypatch, tmp_path: Path):
    # Arrange — 既定の出力先を差し替えて、リポジトリ内の実ファイルを汚さない
    from techradar import openapi_export as module

    default_path = tmp_path / "openapi.json"
    monkeypatch.setattr(module, "DEFAULT_OUTPUT_PATH", default_path)

    # Act
    exit_code = main([])

    # Assert
    assert exit_code == 0
    assert default_path.exists()
