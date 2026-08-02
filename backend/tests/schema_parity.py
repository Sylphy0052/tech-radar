"""SQLAlchemy モデルと Pydantic スキーマの列/フィールド整合性を検証するヘルパー（Issue #18）。

「モデルに列を追加したのに API スキーマへ反映し忘れる」「API スキーマを追加したのに
parity 宣言へ反映し忘れる」の両方を機械的に検出するための検証ロジックだけを置く。
`test_` プレフィックスを付けていないため pytest には収集されない
（実宣言・テストは `tests/test_schema_model_parity.py`、ヘルパー自体のテストは
`tests/test_schema_parity_helpers.py` に分離する）。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import DeclarativeBase

# =============================================================================
# 宣言用データ構造
# =============================================================================


@dataclass(frozen=True)
class ExposedField:
    """モデルの列を公開している Pydantic スキーマ側の (スキーマ, フィールド名)。"""

    schema: type[BaseModel]
    field: str


@dataclass(frozen=True)
class DerivedField:
    """モデル列に直接対応しない、スキーマ側の派生フィールドの宣言。

    `reason` はテストコード上に残す説明（例: 「Recommendation由来ではなく
    user_articles からの導出」）で、検証ロジックはこの値自体を評価しない。
    """

    schema: type[BaseModel]
    field: str
    reason: str


@dataclass(frozen=True)
class ModelParitySpec:
    """1 モデルぶんの「列 → API 露出方針」の宣言。

    `exposed` と `internal` のキー和集合が、モデルの実際の列名集合と
    完全一致していなければならない（過不足・重複いずれも不正）。
    """

    model: type[Any]
    exposed: Mapping[str, Sequence[ExposedField]]
    internal: Mapping[str, str]


# =============================================================================
# モデル列 ⇔ exposed/internal 宣言の検証
# =============================================================================


def _model_columns(model: type[Any]) -> set[str]:
    """SQLAlchemy declarative モデルの実際の列名集合を返す。"""
    return set(model.__table__.columns.keys())


def verify_model_parity(spec: ModelParitySpec) -> list[str]:
    """モデルの列と `exposed` / `internal` 宣言が過不足なく対応しているか検証する。

    検出する不整合:
    - モデルにあるのに `exposed` / `internal` どちらにも宣言されていない列（宣言漏れ）
    - `exposed` / `internal` に宣言されているがモデルに存在しない列
    - `exposed` と `internal` の両方に宣言されている列
    - `exposed` が参照するスキーマにその名前のフィールドが実在しない
    """
    errors: list[str] = []
    model_name = spec.model.__name__
    actual_columns = _model_columns(spec.model)
    exposed_columns = set(spec.exposed)
    internal_columns = set(spec.internal)
    declared_columns = exposed_columns | internal_columns

    missing = actual_columns - declared_columns
    if missing:
        errors.append(f"{model_name}: 宣言漏れの列があります: {sorted(missing)}")

    extra = declared_columns - actual_columns
    if extra:
        errors.append(
            f"{model_name}: 実在しない列が exposed/internal に宣言されています: {sorted(extra)}"
        )

    overlap = exposed_columns & internal_columns
    if overlap:
        errors.append(
            f"{model_name}: exposed と internal の両方に宣言されている列があります: "
            f"{sorted(overlap)}"
        )

    for column, refs in spec.exposed.items():
        for ref in refs:
            if ref.field not in ref.schema.model_fields:
                errors.append(
                    f"{model_name}.{column}: {ref.schema.__name__} にフィールド "
                    f"'{ref.field}' が存在しません"
                )
    return errors


def assert_parity(spec: ModelParitySpec) -> None:
    errors = verify_model_parity(spec)
    assert not errors, "\n".join(errors)


# =============================================================================
# スキーマフィールド ⇔ モデル列/派生フィールドの逆方向検証
# =============================================================================


def verify_schema_field_coverage(
    schema: type[BaseModel],
    specs: Sequence[ModelParitySpec],
    derived: Sequence[DerivedField],
) -> list[str]:
    """スキーマの各フィールドが、モデル列由来か派生フィールドかのいずれかであるか検証する。

    検出する不整合:
    - モデル列にも派生フィールド宣言にも紐付いていないスキーマフィールド（宣言漏れ）
    - スキーマに実在しないフィールド名が exposed / derived から参照されている
    """
    covered: set[str] = set()
    for spec in specs:
        for refs in spec.exposed.values():
            for ref in refs:
                if ref.schema is schema:
                    covered.add(ref.field)
    for derived_field in derived:
        if derived_field.schema is schema:
            covered.add(derived_field.field)

    actual_fields = set(schema.model_fields)
    schema_name = schema.__name__

    errors: list[str] = []
    uncovered = actual_fields - covered
    if uncovered:
        errors.append(
            f"{schema_name}: モデル列にも派生フィールドにも紐付いていないフィールドがあります: "
            f"{sorted(uncovered)}"
        )

    stale = covered - actual_fields
    if stale:
        errors.append(
            f"{schema_name}: 実在しないフィールドが exposed/derived に宣言されています: "
            f"{sorted(stale)}"
        )

    return errors


def assert_schema_coverage(
    schema: type[BaseModel], specs: Sequence[ModelParitySpec], derived: Sequence[DerivedField]
) -> None:
    errors = verify_schema_field_coverage(schema, specs, derived)
    assert not errors, "\n".join(errors)


# =============================================================================
# 「全項目が分類済みか」の汎用検証（モデル一覧 / API スキーマ一覧の両方で使う）
# =============================================================================


def verify_all_classified(
    all_items: Iterable[type[Any]],
    declared: Iterable[type[Any]],
    excluded: Iterable[type[Any]],
    *,
    label: str,
) -> list[str]:
    """`all_items` の全クラスが `declared` か `excluded` のどちらかに属するか検証する。

    新しいモデル/スキーマを追加してどちらにも分類し忘れると、ここで検出される。
    """
    classified = set(declared) | set(excluded)
    unclassified = set(all_items) - classified
    if not unclassified:
        return []
    names = sorted(item.__name__ for item in unclassified)
    return [f"{label}: parity宣言にも除外リストにも無い項目があります: {names}"]


def all_mapped_models(base: type[DeclarativeBase]) -> set[type[Any]]:
    """`base` に紐付く全 SQLAlchemy モデルクラスを返す。"""
    return {mapper.class_ for mapper in base.registry.mappers}


def verify_all_models_classified(
    all_models: Iterable[type[Any]],
    specs: Sequence[ModelParitySpec],
    internal_only: Sequence[type[Any]],
) -> list[str]:
    """全モデルが「parity 宣言を持つモデル」か「内部専用モデル」の
    どちらかに分類されているか検証する。
    """
    return verify_all_classified(
        all_models,
        declared=(spec.model for spec in specs),
        excluded=internal_only,
        label="モデル",
    )


def basemodel_subclasses_defined_in(module: ModuleType) -> set[type[BaseModel]]:
    """`module` 自身で定義された（他モジュールから import しただけではない）
    BaseModel サブクラスを返す。
    """
    return {
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == module.__name__
    }


def basemodel_subclasses_in_package(
    package: ModuleType, extra_modules: Sequence[ModuleType] = ()
) -> set[type[BaseModel]]:
    """`package` 配下の全サブモジュールと `extra_modules` で定義された
    BaseModel サブクラスを集める。

    `techradar.api` のようなパッケージに新しいモジュールを追加しても、この関数は
    `pkgutil.iter_modules` で自動的に拾う（呼び出し側でモジュール名を列挙し直す必要が無い）。
    """
    schemas = basemodel_subclasses_defined_in(package)
    package_path = package.__path__
    for module_info in pkgutil.iter_modules(package_path, prefix=f"{package.__name__}."):
        submodule = importlib.import_module(module_info.name)
        schemas |= basemodel_subclasses_defined_in(submodule)
    for module in extra_modules:
        schemas |= basemodel_subclasses_defined_in(module)
    return schemas


def verify_all_api_schemas_classified(
    all_schemas: Iterable[type[BaseModel]],
    target_schemas: Sequence[type[BaseModel]],
    excluded: Sequence[type[BaseModel]],
) -> list[str]:
    """全 API スキーマが `TARGET_SCHEMAS` か明示的な除外リストの
    どちらかに分類されているか検証する。
    """
    return verify_all_classified(
        all_schemas,
        declared=target_schemas,
        excluded=excluded,
        label="APIスキーマ",
    )
