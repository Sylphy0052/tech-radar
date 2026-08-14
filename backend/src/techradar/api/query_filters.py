"""一覧系 API が共通で使う、クエリパラメータの検証ヘルパ（Issue #91）。

`GET /api/feed`（`recommendations.py`）と `GET /api/articles`（`articles.py`）は、
どちらも自由入力の検索語と topics / technologies を受け取る。上限の値そのものは
エンドポイントごとに定数で持つが、検証の規則は同じなのでここへ集約する。

ただし OFFSET の大きさに関する上限（`MAX_PAGE_NUMBER` と `MAX_OFFSET`）は
この方針の例外で、値そのものをここで共有する。DB の bigint に収まるかどうかという
制約から決まる値であり、エンドポイントごとの事情で変わらないため（Issue #96、#99）。
この2つは `page` を持つ2エンドポイントと、`offset` を直接受け取る
`GET /api/sources` / `GET /api/interests` にそれぞれ効く。
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

# `GET /api/sources` と `GET /api/interests` が受け取る `offset` 直指定の上限
# （Issue #99）。この2エンドポイントは `page` を持たず `offset` を直接クエリ
# パラメータとして受け取るため、`MAX_PAGE_NUMBER` と同じ理屈がそのまま
# `offset` 自身にも当てはまる：上限が無いと巨大な `offset` がそのまま DB へ渡り、
# bigint（signed 64bit）を超えたところで `MAX_PAGE_NUMBER` のコメントに書いた
# 同じ `DataError`（`NumericValueOutOfRange`）が素通りで 500 になる。422 で弾く
# 方式にする理由も同じ（`limit` 側の `le` と扱いを揃える、上限が OpenAPI へ出る、
# 黙って結果をすり替えない）。
#
# 値は `page` 経由で到達できる範囲に合わせた。`page` 側で作れる最大 OFFSET は
# `(MAX_PAGE_NUMBER - 1) * 100`（= 99,999,900）で、これを丸めた 10**8 にしてある。
# 経路が `page` でも `offset` 直指定でも、到達できる範囲がほぼ揃うようにするため
# （同じ bigint 制約から導かれる値であり、エンドポイントごとの事情で変える理由が無い）。
#
# 掛け算を挟まない分、`MAX_PAGE_NUMBER` と違って安全性が `limit` の最大値に
# 依存しない。`config/scoring.yaml` を書き換えてもこの値の妥当性は変わらない。
MAX_OFFSET = 100_000_000


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
