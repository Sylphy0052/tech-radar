import { describe, expect, it } from "vitest";

import { validateArticleUrl } from "@/lib/url-validation";

describe("validateArticleUrl", () => {
  it("returns null for a valid http url", () => {
    // Arrange
    const url = "http://example.com/articles/1";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBeNull();
  });

  it("returns null for a valid https url", () => {
    // Arrange
    const url = "https://example.com/articles/1";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBeNull();
  });

  it("rejects an empty string", () => {
    // Arrange
    const url = "";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBe("URLを入力してください");
  });

  it("rejects a whitespace-only string", () => {
    // Arrange
    const url = "   ";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBe("URLを入力してください");
  });

  it("rejects a url without a scheme before sending the request", () => {
    // Arrange
    const url = "example.com/articles/1";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBe("httpまたはhttpsで始まるURLのみ登録できます");
  });

  it("rejects a non-http(s) scheme such as javascript:", () => {
    // Arrange
    const url = "javascript:alert(1)";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBe("httpまたはhttpsで始まるURLのみ登録できます");
  });

  it("rejects a malformed url that cannot be parsed", () => {
    // Arrange
    const url = "http://";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBe("有効なURLの形式で入力してください");
  });

  it("trims surrounding whitespace before validating", () => {
    // Arrange
    const url = "  https://example.com  ";

    // Act
    const result = validateArticleUrl(url);

    // Assert
    expect(result).toBeNull();
  });
});
