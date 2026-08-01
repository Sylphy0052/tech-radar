"""レジストリのシードと DB 由来の判定を検証する（`PROJECT_SPEC.md` §11）。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from techradar.db import SourceRegistry
from techradar.db.enums import SourceType
from techradar.sources.config import RegistryConfig, load_registry_config
from techradar.sources.service import (
    classify_with_registry,
    load_rules,
    seed_source_registry,
)

SMALL_CONFIG = RegistryConfig.model_validate(
    {
        "authority_by_source_type": {
            "official_documentation": 1.0,
            "official_blog": 0.9,
            "unknown": 0.35,
        },
        "fallback": {"default_source_type": "unknown"},
        "entities": [
            {
                "name": "Example",
                "rules": [
                    {"domain": "docs.example.com", "type": "official_documentation"},
                    {"domain": "example.com", "path": "/blog", "type": "official_blog"},
                ],
            }
        ],
    }
)


class TestSeeding:
    def test_inserts_every_rule(self, db_session: Session):
        # Arrange / Act
        result = seed_source_registry(db_session, SMALL_CONFIG)

        # Assert
        assert result.created == 2
        assert result.updated == 0
        rows = db_session.scalars(select(SourceRegistry)).all()
        assert {row.domain for row in rows} == {"docs.example.com", "example.com"}

    def test_is_idempotent(self, db_session: Session):
        # Arrange — 起動のたびに走らせても行が増えないこと
        seed_source_registry(db_session, SMALL_CONFIG)

        # Act
        result = seed_source_registry(db_session, SMALL_CONFIG)

        # Assert
        assert result.created == 0
        assert result.updated == 0
        assert db_session.scalar(select(func.count()).select_from(SourceRegistry)) == 2

    def test_updates_a_changed_rule(self, db_session: Session):
        # Arrange — 設定側の変更を DB へ反映する
        seed_source_registry(db_session, SMALL_CONFIG)
        row = db_session.scalars(
            select(SourceRegistry).where(SourceRegistry.domain == "docs.example.com")
        ).one()
        row.authority_score = 0.1
        db_session.flush()

        # Act
        result = seed_source_registry(db_session, SMALL_CONFIG)

        # Assert
        assert result.updated == 1
        db_session.refresh(row)
        assert row.authority_score == 1.0

    def test_does_not_overwrite_a_verified_row(self, db_session: Session):
        # Arrange — 手動確認済みの修正をシードが巻き戻さないこと（受入基準）
        seed_source_registry(db_session, SMALL_CONFIG)
        row = db_session.scalars(
            select(SourceRegistry).where(SourceRegistry.domain == "docs.example.com")
        ).one()
        row.authority_score = 0.5
        row.verified = True
        db_session.flush()

        # Act
        result = seed_source_registry(db_session, SMALL_CONFIG)

        # Assert
        assert result.skipped_verified == 1
        db_session.refresh(row)
        assert row.authority_score == 0.5

    def test_seeds_the_bundled_registry(self, db_session: Session):
        # Arrange — 同梱設定を実 DB へ投入できること。
        # github.com の規則は org 違いで多数あり、一意制約の設計が誤っていると落ちる
        config = load_registry_config()

        # Act
        result = seed_source_registry(db_session, config)

        # Assert
        assert result.created == len(config.to_rules())
        github_rows = db_session.scalars(
            select(SourceRegistry).where(SourceRegistry.domain == "github.com")
        ).all()
        assert len({row.github_org for row in github_rows}) == len(github_rows)
        assert len(github_rows) >= 10


class TestClassificationFromTheDatabase:
    def test_uses_the_seeded_rules(self, db_session: Session):
        # Arrange
        seed_source_registry(db_session, SMALL_CONFIG)

        # Act
        result = classify_with_registry(db_session, "https://docs.example.com/guide", SMALL_CONFIG)

        # Assert
        assert result.source_type == SourceType.OFFICIAL_DOCUMENTATION
        assert result.authority_score == 1.0
        assert result.is_primary_source is True

    def test_reflects_a_manual_correction(self, db_session: Session):
        # Arrange — 受入基準「authority を修正でき、以降の判定に反映される」
        seed_source_registry(db_session, SMALL_CONFIG)
        row = db_session.scalars(
            select(SourceRegistry).where(SourceRegistry.domain == "docs.example.com")
        ).one()
        row.source_type = SourceType.TECH_MEDIA.value
        row.authority_score = 0.4
        row.verified = True
        db_session.flush()

        # Act
        result = classify_with_registry(db_session, "https://docs.example.com/guide", SMALL_CONFIG)

        # Assert
        assert result.source_type == SourceType.TECH_MEDIA
        assert result.authority_score == 0.4
        assert result.is_primary_source is False

    def test_falls_back_for_an_unregistered_domain(self, db_session: Session):
        # Arrange
        seed_source_registry(db_session, SMALL_CONFIG)

        # Act
        result = classify_with_registry(
            db_session, "https://unknown.example.org/post", SMALL_CONFIG
        )

        # Assert
        assert result.source_type == SourceType.UNKNOWN
        assert result.authority_score == 0.35

    def test_skips_a_row_with_an_unknown_source_type(self, db_session: Session):
        # Arrange — 手で書き換えられた不正な種別で判定全体を落とさない
        db_session.add(
            SourceRegistry(
                entity_name="Broken",
                domain="broken.example.com",
                source_type="not_a_source_type",
                authority_score=1.0,
            )
        )
        db_session.flush()

        # Act
        rules = load_rules(db_session)

        # Assert
        assert all(rule.domain != "broken.example.com" for rule in rules)

    def test_rejects_an_invalid_authority_score(self, db_session: Session):
        # Arrange — 0.0〜1.0 の範囲外は採用しない
        db_session.add(
            SourceRegistry(
                entity_name="Broken",
                domain="broken2.example.com",
                source_type=SourceType.OFFICIAL_BLOG.value,
                authority_score=5.0,
            )
        )
        db_session.flush()

        # Act
        rules = load_rules(db_session)

        # Assert
        assert all(rule.domain != "broken2.example.com" for rule in rules)


class TestSeedingRejectsBadInput:
    def test_reports_a_duplicate_rule_in_the_configuration(self, db_session: Session):
        # Arrange — 同じ規則を 2 度書いた設定はシード前に弾く
        config = RegistryConfig.model_validate(
            {
                "entities": [
                    {
                        "name": "A",
                        "rules": [{"domain": "dup.example.com", "type": "official_blog"}],
                    },
                    {
                        "name": "B",
                        "rules": [{"domain": "dup.example.com", "type": "official_blog"}],
                    },
                ]
            }
        )

        # Act / Assert
        with pytest.raises(ValueError, match="重複"):
            seed_source_registry(db_session, config)
