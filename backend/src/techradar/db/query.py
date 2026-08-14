"""LIKE / ILIKE パターン組み立ての共通ヘルパ。

検索語をユーザーが自由入力する箇所（`GET /api/feed` の `q`、`GET /api/articles`
の `q`、`GET /api/sources` の `domain` / `entity_name`）はいずれも、検索語を
`ilike` のパターンへ埋め込む前に LIKE の特殊文字（`%` / `_`）をエスケープする
必要がある。エスケープしないと、検索語に含まれるこれらの文字がワイルドカード
として解釈され、意図より広く（あるいは狭く）当たる（Issue #94）。

`api/query_filters.py` は `HTTPException` を送出する API 層のヘルパであり、
`recommendation/service.py`（API 層ではない）から import すると層が逆向きに
なる。LIKE パターンの組み立ては DB のクエリ構築に属する純粋関数なので、
`db/errors.py` と同じ階層のここへ置く。
"""

from __future__ import annotations

LIKE_ESCAPE_CHAR = "\\"


def escape_like_pattern(value: str) -> str:
    r"""LIKE / ILIKE の特殊文字（`%` / `_`）をエスケープする。

    呼び出し側は返り値の前後に `%` を足して部分一致パターンを作り、
    `ilike(pattern, escape="\\")` のように `escape=` を明示して渡すこと
    （エスケープ文字だけ埋め込んで `escape=` を渡し忘れると、挿入した `\%` が
    リテラルのバックスラッシュ + ワイルドカードとして解釈され、
    エスケープしない場合より悪化する）。

    エスケープ文字には `\` を使う。PostgreSQL の `LIKE` は `ESCAPE` 句を
    省略すると既定で `\` を使うが、`standard_conforming_strings` の設定に
    よって解釈が変わりうるため、SQLAlchemy 側の `escape=` で明示的に固定する。

    置換の順序は `\` を先に、`%` と `_` を後に行う。逆にすると、`%` を `\%`
    へ置換した結果生じたバックスラッシュ自身が、続く `\` の置換で
    再びエスケープされて `\\%` になってしまい、リテラルの `\` + ワイルドカード
    として解釈される（二重エスケープ）。1 回の走査で片付けたくなっても、
    この順序依存があるため 1 行へまとめないこと。
    """
    escaped = value.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR * 2)
    escaped = escaped.replace("%", f"{LIKE_ESCAPE_CHAR}%")
    escaped = escaped.replace("_", f"{LIKE_ESCAPE_CHAR}_")
    return escaped
