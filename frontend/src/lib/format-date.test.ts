import { describe, expect, it } from "vitest";

import { formatDateTimeJa } from "@/lib/format-date";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("formatDateTimeJa", () => {
  it("formats an ISO date-time string in Japanese, using JST", () => {
    // Arrange
    const isoString = "2026-08-01T00:00:00Z";

    // Act
    const formatted = formatDateTimeJa(isoString);

    // Assert
    expect(formatted).toBe("2026年8月1日 09:00");
  }, TEST_TIMEOUT_MS);
});
