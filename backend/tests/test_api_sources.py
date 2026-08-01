"""情報源レジストリの管理 API を検証する（`PROJECT_SPEC.md` §11）。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from techradar.api.deps import get_session
from techradar.config import Settings
from techradar.db import SourceRegistry
from techradar.db.enums import SourceType
from techradar.main import create_app
from techradar.sources.config import RegistryConfig
from techradar.sources.service import classify_with_registry

FALLBACK_ONLY_CONFIG = RegistryConfig.model_validate(
    {
        "authority_by_source_type": {"unknown": 0.35},
        "fallback": {"default_source_type": "unknown"},
    }
)


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """テスト用 DB セッションを使う API クライアント。"""
    app = create_app(Settings(_env_file=None))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def make_source(db_session: Session, **overrides: object) -> SourceRegistry:
    """レジストリ 1 件を作る。"""
    values: dict[str, object] = {
        "entity_name": "Example",
        "domain": f"{uuid.uuid4().hex[:8]}.example.com",
        "source_type": SourceType.OFFICIAL_BLOG.value,
        "authority_score": 0.9,
    }
    values.update(overrides)
    row = SourceRegistry(**values)
    db_session.add(row)
    db_session.flush()
    return row


class TestList:
    def test_returns_registered_sources(self, client: TestClient, db_session: Session):
        # Arrange
        make_source(db_session, domain="docs.example.com")

        # Act
        response = client.get("/api/sources")

        # Assert
        assert response.status_code == 200
        assert any(item["domain"] == "docs.example.com" for item in response.json())

    def test_filters_by_domain(self, client: TestClient, db_session: Session):
        # Arrange
        make_source(db_session, domain="docs.filtered.example.com")
        make_source(db_session, domain="blog.other.example.com")

        # Act
        response = client.get("/api/sources", params={"domain": "filtered"})

        # Assert
        domains = [item["domain"] for item in response.json()]
        assert domains == ["docs.filtered.example.com"]

    def test_filters_by_entity_name(self, client: TestClient, db_session: Session):
        # Arrange
        make_source(db_session, entity_name="Anthropic")
        make_source(db_session, entity_name="OpenAI")

        # Act
        response = client.get("/api/sources", params={"entity_name": "anthro"})

        # Assert
        assert [item["entity_name"] for item in response.json()] == ["Anthropic"]

    def test_rejects_an_oversized_page(self, client: TestClient):
        # Arrange / Act — 全件返しでメモリを食い潰させない
        response = client.get("/api/sources", params={"limit": 10_000})

        # Assert
        assert response.status_code == 422


class TestCreate:
    def test_registers_a_source(self, client: TestClient):
        # Arrange
        payload = {
            "entity_name": "Example",
            "domain": "new.example.com",
            "path_pattern": "/docs",
            "source_type": "official_documentation",
            "authority_score": 1.0,
        }

        # Act
        response = client.post("/api/sources", json=payload)

        # Assert
        assert response.status_code == 201
        body = response.json()
        assert body["domain"] == "new.example.com"
        # 手で登録した行はシーダーに上書きさせない
        assert body["verified"] is True

    def test_rejects_a_duplicate(self, client: TestClient, db_session: Session):
        # Arrange
        make_source(db_session, domain="dup.example.com", path_pattern="/docs")

        # Act
        response = client.post(
            "/api/sources",
            json={
                "entity_name": "Example",
                "domain": "dup.example.com",
                "path_pattern": "/docs",
                "source_type": "official_blog",
                "authority_score": 0.9,
            },
        )

        # Assert
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "payload",
        [
            {"authority_score": 1.5},
            {"authority_score": -0.1},
            {"source_type": "not_a_source_type"},
            {"domain": ""},
            {"unexpected_field": "x"},
        ],
    )
    def test_rejects_invalid_input(self, client: TestClient, payload: dict[str, object]):
        # Arrange
        base = {
            "entity_name": "Example",
            "domain": "invalid.example.com",
            "source_type": "official_blog",
            "authority_score": 0.9,
        }

        # Act
        response = client.post("/api/sources", json={**base, **payload})

        # Assert
        assert response.status_code == 422

    def test_does_not_convert_a_non_unique_integrity_error_to_409(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ):
        # Arrange — 一意制約違反 (23505) 以外を 409 に丸めない。将来 FK/CHECK 制約が
        # 増えても、無関係なエラーが「重複登録」という誤ったメッセージにならないこと
        class DummyOrig(Exception):
            sqlstate = "23503"  # foreign_key_violation。一意制約ではない

        def fake_flush() -> None:
            raise IntegrityError("INSERT", {}, DummyOrig("fk violation"))

        monkeypatch.setattr(db_session, "flush", fake_flush)

        # Act / Assert — 409 に変換されず、元の例外がそのまま伝播する（500 相当）
        with pytest.raises(IntegrityError):
            client.post(
                "/api/sources",
                json={
                    "entity_name": "Example",
                    "domain": "boom.example.com",
                    "source_type": "official_blog",
                    "authority_score": 0.9,
                },
            )


class TestUpdate:
    def test_corrects_the_authority_score(self, client: TestClient, db_session: Session):
        # Arrange — 受入基準「PATCH で authority を修正できる」
        row = make_source(db_session, domain="fixme.example.com", authority_score=0.9)

        # Act
        response = client.patch(f"/api/sources/{row.id}", json={"authority_score": 0.4})

        # Assert
        assert response.status_code == 200
        assert response.json()["authority_score"] == 0.4

    def test_marks_the_row_as_verified(self, client: TestClient, db_session: Session):
        # Arrange — 手で直した行をシーダーが巻き戻さないようにする
        row = make_source(db_session, domain="verify.example.com")
        assert row.verified is False

        # Act
        client.patch(f"/api/sources/{row.id}", json={"authority_score": 0.5})

        # Assert
        db_session.refresh(row)
        assert row.verified is True

    def test_reflects_the_correction_in_later_classification(
        self, client: TestClient, db_session: Session
    ):
        # Arrange — 受入基準「修正が以降の判定に反映される」
        row = make_source(
            db_session,
            domain="reflect.example.com",
            source_type=SourceType.OFFICIAL_BLOG.value,
            authority_score=0.9,
        )
        before = classify_with_registry(
            db_session, "https://reflect.example.com/post", FALLBACK_ONLY_CONFIG
        )
        assert before.authority_score == 0.9

        # Act
        client.patch(
            f"/api/sources/{row.id}",
            json={"source_type": "tech_media", "authority_score": 0.45},
        )

        # Assert
        after = classify_with_registry(
            db_session, "https://reflect.example.com/post", FALLBACK_ONLY_CONFIG
        )
        assert after.source_type == SourceType.TECH_MEDIA
        assert after.authority_score == 0.45
        assert after.is_primary_source is False

    def test_leaves_unspecified_fields_untouched(self, client: TestClient, db_session: Session):
        # Arrange — 部分更新が他項目を消さないこと
        row = make_source(
            db_session,
            domain="partial.example.com",
            entity_name="Keep Me",
            path_pattern="/docs",
        )

        # Act
        client.patch(f"/api/sources/{row.id}", json={"authority_score": 0.7})

        # Assert
        db_session.refresh(row)
        assert row.entity_name == "Keep Me"
        assert row.path_pattern == "/docs"

    def test_returns_404_for_an_unknown_id(self, client: TestClient):
        # Arrange / Act
        response = client.patch(f"/api/sources/{uuid.uuid4()}", json={"authority_score": 0.5})

        # Assert
        assert response.status_code == 404

    def test_rejects_a_path_pattern_change(self, client: TestClient, db_session: Session):
        # Arrange — path_pattern は一意キー (domain, path_pattern, github_org) の一部。
        # PATCH で変えられると、シーダーが次回起動時に config 側の原ルールを
        # 別行として再投入してしまい、手直しした行と矛盾する形で併存する
        row = make_source(db_session, domain="keyfield.example.com", path_pattern="/docs")

        # Act
        response = client.patch(f"/api/sources/{row.id}", json={"path_pattern": "/guide"})

        # Assert
        assert response.status_code == 422

    def test_rejects_a_github_org_change(self, client: TestClient, db_session: Session):
        # Arrange — github_org も一意キーの一部で、理由は path_pattern と同じ
        row = make_source(db_session, domain="github.com", github_org="anthropics")

        # Act
        response = client.patch(f"/api/sources/{row.id}", json={"github_org": "openai"})

        # Assert
        assert response.status_code == 422

    def test_rejects_an_out_of_range_score(self, client: TestClient, db_session: Session):
        # Arrange
        row = make_source(db_session, domain="range.example.com")

        # Act
        response = client.patch(f"/api/sources/{row.id}", json={"authority_score": 2.0})

        # Assert
        assert response.status_code == 422
