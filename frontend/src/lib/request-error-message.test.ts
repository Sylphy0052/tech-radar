import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { getRequestErrorMessage } from "@/lib/request-error-message";

describe("getRequestErrorMessage", () => {
  it("returns a server error message for a 5xx ApiError", () => {
    // Arrange
    const error = new ApiError(500, "internal server error");

    // Act
    const message = getRequestErrorMessage(error);

    // Assert
    expect(message).toBe("サーバーでエラーが発生しました。しばらくしてから再度お試しください。");
  });

  it("returns a request error message for a 4xx ApiError", () => {
    // Arrange
    const error = new ApiError(404, "not found");

    // Act
    const message = getRequestErrorMessage(error);

    // Assert
    expect(message).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
  });

  it("returns a network error message for a non-ApiError (e.g. fetch network failure)", () => {
    // Arrange
    const error = new TypeError("Failed to fetch");

    // Act
    const message = getRequestErrorMessage(error);

    // Assert
    expect(message).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
  });

  it("does not throw for a non-Error thrown value", () => {
    // Act
    const message = getRequestErrorMessage("unexpected");

    // Assert
    expect(message).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
  });
});
