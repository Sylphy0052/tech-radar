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

  it("returns a rate limit message with the wait time for a 429 ApiError carrying Retry-After", () => {
    // Arrange
    const error = new ApiError(429, "too many requests", 30);

    // Act
    const message = getRequestErrorMessage(error);

    // Assert
    expect(message).toBe("リクエストが多すぎます。約30秒後に再度お試しください。");
  });

  it("omits the wait time when Retry-After is 0 seconds", () => {
    // Arrange — 境界ちょうどで弾かれると backend は 0 を返しうる
    const error = new ApiError(429, "too many requests", 0);

    // Act
    const message = getRequestErrorMessage(error);

    // Assert
    expect(message).toBe("リクエストが多すぎます。しばらくしてから再度お試しください。");
  });

  it("returns a rate limit message without a wait time when Retry-After is missing", () => {
    // Arrange
    const error = new ApiError(429, "too many requests");

    // Act
    const message = getRequestErrorMessage(error);

    // Assert
    expect(message).toBe("リクエストが多すぎます。しばらくしてから再度お試しください。");
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
