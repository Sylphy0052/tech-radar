"""一覧系 API が共通で使う、クエリパラメータの検証ヘルパ（Issue #91）。

`GET /api/feed`（`recommendations.py`）と `GET /api/articles`（`articles.py`）は、
どちらも自由入力の検索語と topics / technologies を受け取る。上限の値そのものは
エンドポイントごとに定数で持つが、検証の規則は同じなのでここへ集約する。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import HTTPException, status


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
