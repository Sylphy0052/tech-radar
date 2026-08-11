import { afterEach, describe, expect, it, vi } from "vitest";

import { reportUnexpectedState } from "@/lib/report-unexpected";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("reportUnexpectedState", () => {
  it("warns with the given message outside production", () => {
    // Arrange
    vi.stubEnv("NODE_ENV", "development");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    // Act
    reportUnexpectedState("未知のarticle_id: abc");

    // Assert
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0]?.[0]).toContain("未知のarticle_id: abc");
  }, TEST_TIMEOUT_MS);

  it("stays silent in production so users never see an internal message", () => {
    // Arrange
    vi.stubEnv("NODE_ENV", "production");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    // Act
    reportUnexpectedState("未知のarticle_id: abc");

    // Assert
    expect(warn).not.toHaveBeenCalled();
  }, TEST_TIMEOUT_MS);
});
