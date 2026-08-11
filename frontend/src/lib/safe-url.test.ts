import { describe, expect, it } from "vitest";

import { isSafeHttpUrl } from "@/lib/safe-url";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("isSafeHttpUrl", () => {
  it("returns true for an https url", () => {
    // Arrange / Act / Assert
    expect(isSafeHttpUrl("https://example.com/articles/1")).toBe(true);
  }, TEST_TIMEOUT_MS);

  it("returns true for an http url", () => {
    // Arrange / Act / Assert
    expect(isSafeHttpUrl("http://example.com/articles/1")).toBe(true);
  }, TEST_TIMEOUT_MS);

  it("returns false for a javascript: url", () => {
    // Arrange / Act / Assert
    expect(isSafeHttpUrl("javascript:alert(1)")).toBe(false);
  }, TEST_TIMEOUT_MS);

  it("returns false for a data: url", () => {
    // Arrange / Act / Assert
    expect(isSafeHttpUrl("data:text/html,<script>alert(1)</script>")).toBe(false);
  }, TEST_TIMEOUT_MS);

  it("returns false for a string without a valid URL structure", () => {
    // Arrange / Act / Assert
    expect(isSafeHttpUrl("not a url")).toBe(false);
  }, TEST_TIMEOUT_MS);

  it("returns false for an empty string", () => {
    // Arrange / Act / Assert
    expect(isSafeHttpUrl("")).toBe(false);
  }, TEST_TIMEOUT_MS);
});
