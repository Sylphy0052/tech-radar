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
    `ilike(pattern, escape=LIKE_ESCAPE_CHAR)` のように `escape=` を明示して渡すこと
    （エスケープ文字をリテラルで書かず、この定数を参照すること）。

    エスケープ文字には `\` を使う。PostgreSQL の `LIKE` / `ILIKE` は `ESCAPE`
    句を省略しても既定で `\` をエスケープ文字として扱うため、現時点では
    `escape=` の有無で実行結果は変わらない（2026-08-14 実測）。

    ```
    docker exec -i techradar-postgres psql -U techradar -d techradar -c \
      "SELECT '100%' ILIKE '100\%' AS literal_match, '100x' ILIKE '100\%' AS should_be_false;"
    -- literal_match=t, should_be_false=f （ESCAPE句を省略しても \ がエスケープとして働く）
    ```

    `standard_conforming_strings` は SQL 文字列リテラルの構文解析設定であり、
    値をバインドパラメータで渡すこのコードのパスには影響しない（`SHOW
    standard_conforming_strings` は `on` で、上記の結果はこの設定と無関係に
    再現する）。

    それでも `escape=` を明示するのは、この既定に依存しないための防御的な
    記述である。PostgreSQL 以外の DB への移行、SQLAlchemy 側の既定挙動の
    変更、将来エスケープ文字そのものを変える判断のいずれからも独立させたい。
    実際、SQLAlchemy は `escape=` を渡さないと生成 SQL に `ESCAPE` 句自体を
    含めない（`ilike('100\%')` は `title ILIKE '100\%'` になり、`ESCAPE '\'`
    が付かない）。PostgreSQL の既定と一致してはいるが、コンパイル結果として
    `ESCAPE` 句が無いことに変わりはないため、明示しておく。

    このため「`escape=` を外す」という壊し方は、このスタックではテストで
    検知できない。PostgreSQL の既定と一致しているため挙動が変わらず、
    アサーションが落ちない。将来この引数を誤って削っても、実行結果からは
    気付けない前提で読むこと（テストが無いのは漏れではなく、検知不能な
    区分であるため）。

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
