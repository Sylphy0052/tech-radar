"""SQLAlchemy モデルと Pydantic スキーマの列/フィールド整合性を検証するヘルパー（Issue #18）。

「モデルに列を追加したのに API スキーマへ反映し忘れる」「API スキーマを追加したのに
parity 宣言へ反映し忘れる」の両方を機械的に検出するための検証ロジックだけを置く。
`test_` プレフィックスを付けていないため pytest には収集されない
（実宣言・テストは `tests/test_schema_model_parity.py`、ヘルパー自体のテストは
`tests/test_schema_parity_helpers.py` に分離する）。

**このテスト機構が保証する範囲（限界）**:

- green であることが保証するのは「モデルの列が exposed/internal のどちらかに
  分類されている」「スキーマのフィールドがモデル列/派生フィールドのいずれかに
  紐付いている」という**構造的な宣言の完全性**のみ。その分類・公開判断が
  セキュリティ上妥当かどうかは検証しない。`embedding` / `payload` / `user_id` の
  ような機微な列を `internal` から `exposed` へ移し、対応する Pydantic
  フィールドを追加するだけでこのテストは green のまま通る。機微な列を
  `exposed` へ追加・移動する差分は security-auditor によるレビューを必須とする
- 検証するのは列とフィールドの「存在対応」のみで、実際の値の詰め替えロジックの
  正しさ（例: `canonical_url` と `original_url` を取り違えて代入している等）は
  検証しない。値レベルの整合は API 統合テスト（`test_api_*.py`）の責務とする
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any, get_args, get_origin

from fastapi import FastAPI
from fastapi.routing import APIRoute
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

    **限界**: この宣言と `verify_model_parity` が保証するのは列の分類漏れが
    無いことだけであり、`exposed` への分類そのものが安全かどうかは判定しない。
    機微な列を `internal` から `exposed` へ移す変更は、この宣言を更新すれば
    テストは通ってしまうため、必ず security-auditor によるレビューを経ること。
    """

    model: type[DeclarativeBase]
    exposed: Mapping[str, Sequence[ExposedField]]
    internal: Mapping[str, str]


# =============================================================================
# モデル列 ⇔ exposed/internal 宣言の検証
# =============================================================================


def _model_columns(model: type[DeclarativeBase]) -> set[str]:
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
    - 異なる (モデル, 列) から同じスキーマフィールドへ二重に exposed 宣言されている

    最後の項目のため、`covered` はフィールド名の単純な集合ではなく
    「フィールド名 → 寄与元 (モデル名, 列名) のリスト」として持つ。set への
    追加は冪等なため、単純な `set[str]` では別モデルの別列が誤って同じ
    フィールド名を指しても素通りしてしまう。
    """
    contributors: dict[str, list[tuple[str, str]]] = {}
    for spec in specs:
        model_name = spec.model.__name__
        for column, refs in spec.exposed.items():
            for ref in refs:
                if ref.schema is schema:
                    contributors.setdefault(ref.field, []).append((model_name, column))

    derived_fields = {
        derived_field.field for derived_field in derived if derived_field.schema is schema
    }
    covered = set(contributors) | derived_fields

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

    for field, sources in contributors.items():
        distinct_sources = sorted(set(sources))
        if len(distinct_sources) > 1:
            errors.append(
                f"{schema_name}.{field}: 異なる列から二重に exposed 宣言されています: "
                f"{distinct_sources}"
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
    モデル（`DeclarativeBase` 派生）と API スキーマ（`BaseModel` 派生）の両方で
    使う汎用ヘルパーのため、型は意図的に `type[Any]` のままにしている。
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
    """`module` 自身で定義された BaseModel サブクラスを返す。

    `inspect.getmembers` はモジュールのトップレベル属性（モジュール名前空間に
    束縛された名前）だけを見るため、対象はモジュールのトップレベルで定義された
    クラスに限られる。他モジュールから import しただけのクラス（`obj.__module__`
    がこのモジュールと一致しないもの）や、関数内でローカルに定義され
    モジュール属性として束縛されていないクラスは、そもそも `getmembers` の
    走査対象に現れないため拾わない。
    """
    return {
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == module.__name__
    }


def basemodel_subclasses_in_package(
    package: ModuleType, extra_modules: Sequence[ModuleType] = ()
) -> set[type[BaseModel]]:
    """`package` 配下の全サブモジュール（サブパッケージを含め再帰的に）と
    `extra_modules` で定義された BaseModel サブクラスを集める。

    `pkgutil.walk_packages` を使うため、`techradar.api.v2` のようなサブ
    パッケージを将来追加しても、この関数は自動的に配下まで辿って拾う
    （呼び出し側でモジュール名を列挙し直す必要が無い）。

    **前提**: `importlib.import_module` で対象モジュールを動的 import する。
    走査対象のパッケージ配下には信頼できる自前コードのみを置くこと
    （import 時に副作用を持つ外部/生成コードを置くと、このテストの実行時に
    その副作用が発生する）。
    """
    schemas = basemodel_subclasses_defined_in(package)
    package_path = package.__path__
    for module_info in pkgutil.walk_packages(package_path, prefix=f"{package.__name__}."):
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


# =============================================================================
# FastAPI ルーティングから実参照されているスキーマの収集
# =============================================================================
#
# パッケージ配下の走査（basemodel_subclasses_in_package）はモジュール配置に
# 依存するため、新しいエンドポイントのスキーマを techradar.api / techradar.main
# 以外のモジュール（別パッケージの router、共通 DTO モジュール等）へ置くと
# 静かにすり抜ける。ここでは FastAPI アプリの実際のルーティング情報
# （response_model・リクエストボディ・responses=）から辿ることで、
# モジュール配置に依存しない網羅性を提供する。


def _iter_api_routes(app: FastAPI) -> Iterator[APIRoute]:
    """`app` に登録された全 `APIRoute` を辿る。

    このプロジェクトの FastAPI（0.141 系）は `include_router()` したルーターを
    `_IncludedRouter`（実体は `original_router` 属性に持つ）として
    `app.routes` へ積む遅延合成方式になっており、標準的な
    `isinstance(route, APIRoute)` による平坦な走査だけでは `/api/sources` 等の
    サブルーター配下を拾えない（`backend/AGENTS.md` 相当の非互換、実機で確認
    済み）。`original_router.routes` と通常の `routes` 属性（Starlette の
    `Mount` 等）の両方を再帰的に辿ることで両ケースに対応する。
    """
    pending: list[Any] = list(app.routes)
    while pending:
        route = pending.pop()
        if isinstance(route, APIRoute):
            yield route
            continue
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(original_router.routes)
            continue
        nested_routes = getattr(route, "routes", None)
        if nested_routes is not None:
            pending.extend(nested_routes)


def _unwrap_basemodel_types(annotation: Any) -> set[type[BaseModel]]:
    """型注釈から参照されている BaseModel サブクラスを再帰的に取り出す。

    `list[X]` / `X | None` / `dict[str, X]` のようなジェネリックを
    `typing.get_origin` / `get_args` で分解し、内側の型引数も再帰的に見る。
    """
    if annotation is None:
        return set()
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {annotation}
    origin = get_origin(annotation)
    if origin is None:
        return set()
    result: set[type[BaseModel]] = set()
    for arg in get_args(annotation):
        result |= _unwrap_basemodel_types(arg)
    return result


def _is_framework_synthesized_body_model(schema: type[BaseModel]) -> bool:
    """FastAPI が multipart/form-data のボディ用に動的生成する wrapper モデルかを判定する。

    `UploadFile` / `File(...)` を引数に取るエンドポイント（`POST /api/articles/bulk`
    等）では、FastAPI が `fastapi._compat` 配下に
    `Body_<関数名>_<パス>_<メソッド>` という名前のクラスを実行時に生成し、
    `route.body_field.field_info.annotation` へ差し込む（実機で確認済み）。
    これはアプリ側が定義した実在のスキーマではなく、フィールドの実体も
    `UploadFile` であり DB モデルの列に対応しようがないため、
    `TARGET_SCHEMAS` / `EXCLUDED_API_SCHEMAS` のどちらにも宣言させず、
    モジュール名（`fastapi.` 配下）で機械的に除外する。
    """
    return schema.__module__.startswith("fastapi.")


def schemas_reachable_from_app(app: FastAPI) -> set[type[BaseModel]]:
    """`app` の全ルートから実際に参照されている BaseModel を集める。

    対象は `response_model`、リクエストボディの型、`responses=` に指定された
    追加スキーマ（429 応答の `RateLimitedResponse` 等）。FastAPI が multipart
    ボディ用に動的生成する wrapper モデルは対象外にする
    （`_is_framework_synthesized_body_model` 参照）。

    **ネストしたモデルは辿らない**（例: `RecommendationItem.feedback` が持つ
    `ArticleFeedbackResponse`）。ネスト先のフィールドはそのスキーマ自身が
    `TARGET_SCHEMAS` に個別登録されていれば `verify_schema_field_coverage` が
    検証するため、ここでは「ルートから直接参照されるトップレベルスキーマ」の
    網羅性だけを見れば、モジュール配置に依存しない検証という目的には十分と判断した。
    """
    schemas: set[type[BaseModel]] = set()
    for route in _iter_api_routes(app):
        if route.response_model is not None:
            schemas |= _unwrap_basemodel_types(route.response_model)
        if route.body_field is not None:
            schemas |= _unwrap_basemodel_types(route.body_field.field_info.annotation)
        for response_spec in route.responses.values():
            model = response_spec.get("model")
            if model is not None:
                schemas |= _unwrap_basemodel_types(model)
    return {schema for schema in schemas if not _is_framework_synthesized_body_model(schema)}
