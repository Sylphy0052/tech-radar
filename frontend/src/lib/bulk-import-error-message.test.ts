import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { getBulkImportErrorMessage } from "@/lib/bulk-import-error-message";

describe("getBulkImportErrorMessage", () => {
  it("returns the size/count limit message for a 413 ApiError", () => {
    // Arrange
    const error = new ApiError(413, "payload too large");

    // Act
    const message = getBulkImportErrorMessage(error);

    // Assert
    expect(message).toBe(
      "ファイルが大きすぎるか、URLの件数が多すぎます（1MB以内、500件以内にしてください）",
    );
  });

  it("returns the unsupported format message for a 422 ApiError", () => {
    // Arrange
    const error = new ApiError(422, "unprocessable entity");

    // Act
    const message = getBulkImportErrorMessage(error);

    // Assert
    expect(message).toBe(
      "対応していないファイル形式です（.md / .txt のUTF-8テキストのみ対応しています）",
    );
  });

  it("delegates to getRequestErrorMessage for a 5xx ApiError", () => {
    // Arrange
    const error = new ApiError(500, "internal server error");

    // Act
    const message = getBulkImportErrorMessage(error);

    // Assert
    expect(message).toBe("サーバーでエラーが発生しました。しばらくしてから再度お試しください。");
  });

  it("delegates to getRequestErrorMessage for a 429 ApiError carrying Retry-After", () => {
    // Arrange
    const error = new ApiError(429, "too many requests", 30);

    // Act
    const message = getBulkImportErrorMessage(error);

    // Assert
    expect(message).toBe("リクエストが多すぎます。約30秒後に再度お試しください。");
  });

  it("delegates to getRequestErrorMessage for a non-ApiError (e.g. network failure)", () => {
    // Arrange
    const error = new TypeError("Failed to fetch");

    // Act
    const message = getBulkImportErrorMessage(error);

    // Assert
    expect(message).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
  });
});
