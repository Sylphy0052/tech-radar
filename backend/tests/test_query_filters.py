"""`api/query_filters` の単体テスト（Issue #91、Issue #96）。

このヘルパは `GET /api/feed` と `GET /api/articles` の両方から呼ばれるため、
エンドポイント経由の間接テストだけだと、3つ目の呼び出し元が増えたときに
規則が変わったことへ気付きにくい。ここでは関数を直接呼ぶ。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from techradar.api.articles import MAX_INTEREST_LIST_PAGE_SIZE
from techradar.api.query_filters import MAX_PAGE_NUMBER, reject_oversized_list
from techradar.api.recommendations import MAX_PAGE_SIZE


class TestRejectOversizedList:
    @pytest.mark.parametrize("values", [None, [], ["llm", "rag"]])
    def test_accepts_values_within_the_limits(self, values: list[str] | None) -> None:
        # Arrange & Act & Assert — 未指定・空・上限内はいずれも素通りする
        reject_oversized_list(values, param_name="topics", max_items=2, max_item_length=8)

    def test_rejects_too_many_items(self) -> None:
        # Arrange
        values = ["a", "b", "c"]

        # Act
        with pytest.raises(HTTPException) as exc_info:
            reject_oversized_list(values, param_name="topics", max_items=2, max_item_length=8)

        # Assert
        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "topics" in str(exc_info.value.detail)

    def test_rejects_a_too_long_item(self) -> None:
        # Arrange — 件数は上限内で、要素の長さだけが超える
        values = ["ok", "x" * 9]

        # Act
        with pytest.raises(HTTPException) as exc_info:
            reject_oversized_list(values, param_name="technologies", max_items=2, max_item_length=8)

        # Assert
        assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "technologies" in str(exc_info.value.detail)

    def test_reports_the_parameter_name_it_was_given(self) -> None:
        # Arrange — メッセージを見た利用者が、どちらのパラメータかを判別できること
        with pytest.raises(HTTPException) as exc_info:
            reject_oversized_list(
                ["a", "b"], param_name="technologies", max_items=1, max_item_length=8
            )

        # Assert
        assert "technologies" in str(exc_info.value.detail)


class TestMaxPageNumber:
    """`MAX_PAGE_NUMBER` が bigint に収まる OFFSET しか作らないこと（Issue #96）。

    1,000,000 という値を選んだ根拠は「`limit` の最大値と掛けても OFFSET が bigint に
    収まる」ことだが、掛ける相手のうち `GET /api/feed` 側の `MAX_PAGE_SIZE` は
    `config/scoring.yaml` から実行時に読む。設定を書き換えると根拠が黙って崩れて、
    修正前と同じ 500 に戻る。コメントだけに残さず機械で押さえる。
    """

    BIGINT_MAX = 2**63 - 1

    @pytest.mark.parametrize(
        "max_page_size",
        [MAX_PAGE_SIZE, MAX_INTEREST_LIST_PAGE_SIZE],
        ids=["feed", "interest_articles"],
    )
    def test_the_largest_offset_fits_in_a_bigint(self, max_page_size: int) -> None:
        # Arrange — 各エンドポイントが組み立てうる最大の OFFSET
        largest_offset = (MAX_PAGE_NUMBER - 1) * max_page_size

        # Assert
        assert largest_offset <= TestMaxPageNumber.BIGINT_MAX
