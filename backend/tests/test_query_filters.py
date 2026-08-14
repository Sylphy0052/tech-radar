"""`api/query_filters.reject_oversized_list` の単体テスト（Issue #91）。

このヘルパは `GET /api/feed` と `GET /api/articles` の両方から呼ばれるため、
エンドポイント経由の間接テストだけだと、3つ目の呼び出し元が増えたときに
規則が変わったことへ気付きにくい。ここでは関数を直接呼ぶ。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from techradar.api.query_filters import reject_oversized_list


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
