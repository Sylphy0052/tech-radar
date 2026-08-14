"""一覧系 API が共通で使う、クエリパラメータの検証ヘルパ（Issue #91）。

`GET /api/feed`（`recommendations.py`）と `GET /api/articles`（`articles.py`）は、
どちらも自由入力の検索語と topics / technologies を受け取る。上限の値そのものは
エンドポイントごとに定数で持つが、検証の規則は同じなのでここへ集約する。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException, status

# 両エンドポイントに共通の `page` の上限（Issue #96）。両方とも
# `.offset((page - 1) * limit)` で OFFSET を組み立てるため、上限が無いと
# 巨大な page で bigint（signed 64bit）を超える OFFSET がそのまま DB へ渡る。
# 本番 DB へ直接投げて実測した結果は次のとおり：OFFSET は 2**63 - 1
# （bigint の最大値）までは例外にならず 0 件で成功し、2**63 を超えると
# `sqlalchemy.exc.DataError`（`psycopg.errors.NumericValueOutOfRange`、
# bigint out of range）になる。FastAPI 側で捕まえていないため素通りで 500 になる。
#
# offset のクランプや最終ページへの丸めではなく 422 で弾く方式にしたのは、
# `limit` 側が既に `le` で 422 を返しており扱いを揃えられること、上限が
# OpenAPI スキーマへ出ること、誤った URL を黙って別の結果へすり替えないこと
# による（採用しない案の比較）。
#
# 1,000,000 という値は、`limit` の最大値（大きい方で100）と掛けても
# offset が 10**8 に収まり、実測どおり DB が即座に返せる範囲であることから
# 決めた安全弁。現在の記事数は261件で、実用上はまず到達しない。
MAX_PAGE_NUMBER = 1_000_000


def reject_oversized_list(
    values: Sequence[str] | None,
    *,
    param_name: str,
    max_items: int,
    max_item_length: int,
) -> None:
    """`topics` / `technologies` の件数・要素ごとの長さを検証し、超過なら 422。

    FastAPI の `Query(max_length=...)` は `list[str]` の要素単位には効かず（リスト
    自体にしか効かない、実機で確認済み）、件数の制約を表す機能も無い。そのため
    `api/articles.py` の `_reject_naive_datetime` と同じ「関数本体で明示チェックして
    `HTTPException` を送出する」方式で検証する（Issue #90 自己レビュー）。
    """
    if not values:
        return
    if len(values) > max_items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{param_name} の件数が上限（{max_items}件）を超えています",
        )
    for value in values:
        if len(value) > max_item_length:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{param_name} の要素は{max_item_length}文字以下にしてください",
            )
