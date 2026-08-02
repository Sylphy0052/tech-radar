"""`tests/schema_parity.py` の検証ヘルパー自体のテスト（Issue #18）。

「列を追加して宣言し忘れたら落ちる」を保証しているのはこのヘルパーのロジック自体
なので、実モデル・実スキーマとは切り離してヘルパー単体の RED/GREEN をここで検証する。
ダミーモデルは実 DB のメタデータ（`techradar.db.base.Base`）を汚さないよう、
専用の `DeclarativeBase` / `MetaData` を使う。
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tests.schema_parity import (
    DerivedField,
    ExposedField,
    ModelParitySpec,
    verify_all_api_schemas_classified,
    verify_all_models_classified,
    verify_model_parity,
    verify_schema_field_coverage,
)

# =============================================================================
# テスト用のダミーモデル・ダミースキーマ
# =============================================================================


class _DummyDeclarativeBase(DeclarativeBase):
    """ヘルパーのテスト専用の declarative base。実 DB のメタデータを汚さない。"""

    metadata = MetaData()


class _DummyModel(_DummyDeclarativeBase):
    """検証対象にする使い捨てのモデル。"""

    __tablename__ = "dummy_models_for_parity_test"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    secret: Mapped[str] = mapped_column(String(50))


class _DummySchema(BaseModel):
    """検証対象にする使い捨ての Pydantic スキーマ。"""

    id: int
    name: str


class _DummySchemaWithComputedField(BaseModel):
    """モデル列由来でない計算済みフィールドを持つ使い捨てスキーマ。"""

    id: int
    computed: str


def _complete_dummy_spec() -> ModelParitySpec:
    """`_DummyModel` の全列を過不足なく宣言した、正しい状態の spec。"""
    return ModelParitySpec(
        model=_DummyModel,
        exposed={
            "id": (ExposedField(_DummySchema, "id"),),
            "name": (ExposedField(_DummySchema, "name"),),
        },
        internal={"secret": "テスト用の非公開列"},
    )


# =============================================================================
# verify_model_parity
# =============================================================================


class TestVerifyModelParity:
    """`verify_model_parity` のテスト。"""

    def test_returns_no_errors_when_all_columns_are_declared(self) -> None:
        # Arrange
        spec = _complete_dummy_spec()
        # Act
        errors = verify_model_parity(spec)
        # Assert
        assert errors == []

    def test_fails_when_a_column_is_not_declared_anywhere(self) -> None:
        """モデルに列を足して宣言し忘れた状況を再現する。"""
        # Arrange: secret 列の宣言を落とす（宣言漏れ）。
        spec = ModelParitySpec(
            model=_DummyModel,
            exposed={
                "id": (ExposedField(_DummySchema, "id"),),
                "name": (ExposedField(_DummySchema, "name"),),
            },
            internal={},
        )
        # Act
        errors = verify_model_parity(spec)
        # Assert
        assert any("宣言漏れ" in error and "secret" in error for error in errors)

    def test_fails_when_a_declared_column_does_not_exist_on_the_model(self) -> None:
        # Arrange
        spec = ModelParitySpec(
            model=_DummyModel,
            exposed={
                "id": (ExposedField(_DummySchema, "id"),),
                "name": (ExposedField(_DummySchema, "name"),),
                "not_a_real_column": (ExposedField(_DummySchema, "name"),),
            },
            internal={"secret": "テスト用の非公開列"},
        )
        # Act
        errors = verify_model_parity(spec)
        # Assert
        assert any("実在しない列" in error and "not_a_real_column" in error for error in errors)

    def test_fails_when_a_column_is_declared_both_exposed_and_internal(self) -> None:
        # Arrange
        spec = ModelParitySpec(
            model=_DummyModel,
            exposed={
                "id": (ExposedField(_DummySchema, "id"),),
                "name": (ExposedField(_DummySchema, "name"),),
                "secret": (ExposedField(_DummySchema, "name"),),
            },
            internal={"secret": "テスト用の非公開列"},
        )
        # Act
        errors = verify_model_parity(spec)
        # Assert
        assert any("両方に宣言" in error and "secret" in error for error in errors)

    def test_fails_when_exposed_field_name_does_not_exist_on_the_schema(self) -> None:
        """スキーマに存在しないフィールド名を exposed に宣言した状況を再現する。"""
        # Arrange
        spec = ModelParitySpec(
            model=_DummyModel,
            exposed={
                "id": (ExposedField(_DummySchema, "id"),),
                # _DummySchema に存在しないフィールド名を誤って宣言する。
                "name": (ExposedField(_DummySchema, "nonexistent_field"),),
            },
            internal={"secret": "テスト用の非公開列"},
        )
        # Act
        errors = verify_model_parity(spec)
        # Assert
        assert any("nonexistent_field" in error and "_DummySchema" in error for error in errors)


# =============================================================================
# verify_schema_field_coverage
# =============================================================================


class TestVerifySchemaFieldCoverage:
    """`verify_schema_field_coverage` のテスト。"""

    def test_returns_no_errors_when_every_field_is_covered(self) -> None:
        # Arrange
        specs = (_complete_dummy_spec(),)
        # Act
        errors = verify_schema_field_coverage(_DummySchema, specs, derived=())
        # Assert
        assert errors == []

    def test_fails_when_a_schema_field_has_no_column_or_derived_declaration(self) -> None:
        """モデル由来でも派生宣言済みでもないフィールドを持つスキーマを検出する。"""
        # Arrange: id だけモデル列に対応させ、computed は未宣言のまま残す。
        spec = ModelParitySpec(
            model=_DummyModel,
            exposed={"id": (ExposedField(_DummySchemaWithComputedField, "id"),)},
            internal={"name": "テスト用未使用列", "secret": "テスト用の非公開列"},
        )
        # Act
        errors = verify_schema_field_coverage(_DummySchemaWithComputedField, (spec,), derived=())
        # Assert
        assert any("computed" in error for error in errors)

    def test_passes_once_the_computed_field_is_declared_as_derived(self) -> None:
        """派生フィールドとして明示宣言すれば検証が通るようになることを確認する（GREEN）。"""
        # Arrange
        spec = ModelParitySpec(
            model=_DummyModel,
            exposed={"id": (ExposedField(_DummySchemaWithComputedField, "id"),)},
            internal={"name": "テスト用未使用列", "secret": "テスト用の非公開列"},
        )
        derived = (
            DerivedField(_DummySchemaWithComputedField, "computed", "テスト用の派生フィールド"),
        )
        # Act
        errors = verify_schema_field_coverage(_DummySchemaWithComputedField, (spec,), derived)
        # Assert
        assert errors == []


# =============================================================================
# verify_all_models_classified
# =============================================================================


class TestVerifyAllModelsClassified:
    """`verify_all_models_classified` のテスト。"""

    def test_returns_no_errors_when_every_model_is_classified(self) -> None:
        # Arrange
        spec = _complete_dummy_spec()

        class _OtherInternalModel:
            pass

        # Act
        errors = verify_all_models_classified(
            all_models=(_DummyModel, _OtherInternalModel),
            specs=(spec,),
            internal_only=(_OtherInternalModel,),
        )
        # Assert
        assert errors == []

    def test_fails_when_a_model_belongs_to_neither_category(self) -> None:
        """新しいモデルを追加して分類し忘れた状況を再現する。"""

        # Arrange
        class _UnclassifiedModel:
            pass

        # Act
        errors = verify_all_models_classified(
            all_models=(_DummyModel, _UnclassifiedModel),
            specs=(_complete_dummy_spec(),),
            internal_only=(),
        )
        # Assert
        assert any("_UnclassifiedModel" in error for error in errors)


# =============================================================================
# verify_all_api_schemas_classified
# =============================================================================


class TestVerifyAllApiSchemasClassified:
    """`verify_all_api_schemas_classified` のテスト。"""

    def test_returns_no_errors_when_every_schema_is_classified(self) -> None:
        # Arrange

        class _ExcludedSchema(BaseModel):
            pass

        # Act
        errors = verify_all_api_schemas_classified(
            all_schemas=(_DummySchema, _ExcludedSchema),
            target_schemas=(_DummySchema,),
            excluded=(_ExcludedSchema,),
        )
        # Assert
        assert errors == []

    def test_fails_when_a_schema_belongs_to_neither_category(self) -> None:
        """新しい API スキーマを追加して TARGET_SCHEMAS へ足し忘れた状況を再現する。"""

        # Arrange
        class _ForgottenSchema(BaseModel):
            pass

        # Act
        errors = verify_all_api_schemas_classified(
            all_schemas=(_DummySchema, _ForgottenSchema),
            target_schemas=(_DummySchema,),
            excluded=(),
        )
        # Assert
        assert any("_ForgottenSchema" in error for error in errors)
