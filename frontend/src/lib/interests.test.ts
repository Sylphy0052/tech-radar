import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DIFFICULTY_LABELS,
  formatNullableLabel,
  formatWeekLabel,
  getInterestSummary,
  getInterestTimeline,
  listInterestClusters,
} from "@/lib/interests";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("formatNullableLabel", () => {
  it("returns 未分類 when the value is null", () => {
    expect(formatNullableLabel(null)).toBe("未分類");
  }, TEST_TIMEOUT_MS);

  it("returns 未分類 when the value is null even with a label map", () => {
    expect(formatNullableLabel(null, DIFFICULTY_LABELS)).toBe("未分類");
  }, TEST_TIMEOUT_MS);

  it("returns the mapped label when a label map is given", () => {
    expect(formatNullableLabel("beginner", DIFFICULTY_LABELS)).toBe("初級");
  }, TEST_TIMEOUT_MS);

  it("falls back to the raw value when the label map has no entry", () => {
    expect(formatNullableLabel("unknown-difficulty", DIFFICULTY_LABELS)).toBe("unknown-difficulty");
  }, TEST_TIMEOUT_MS);

  it("returns the raw value as-is when no label map is given", () => {
    expect(formatNullableLabel("qiita.com")).toBe("qiita.com");
  }, TEST_TIMEOUT_MS);
});

describe("formatWeekLabel", () => {
  it("formats an ISO week-start datetime as a short month/day label", () => {
    expect(formatWeekLabel("2026-08-01T00:00:00Z")).toBe("8/1週");
  }, TEST_TIMEOUT_MS);
});

describe("getInterestSummary", () => {
  it("requests GET /api/interests/summary", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        genres: [],
        feedback_ratio: { good_count: 0, bad_count: 0, save_count: 0 },
        technologies: [],
        primary_source_ratio: { primary_count: 0, secondary_count: 0 },
        content_types: [],
        difficulties: [],
        suppressed_topics: [],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getInterestSummary();

    // Assert
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/interests/summary"),
      expect.anything(),
    );
  }, TEST_TIMEOUT_MS);
});

describe("listInterestClusters", () => {
  it("requests GET /api/interests/clusters", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await listInterestClusters();

    // Assert
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/interests/clusters"),
      expect.anything(),
    );
  }, TEST_TIMEOUT_MS);
});

describe("getInterestTimeline", () => {
  it("requests GET /api/interests/timeline without a query when weeks is omitted", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ buckets: [] }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getInterestTimeline();

    // Assert
    const requestedUrl = fetchMock.mock.calls[0]?.[0] as string;
    expect(requestedUrl.endsWith("/api/interests/timeline")).toBe(true);
  }, TEST_TIMEOUT_MS);

  it("appends weeks as a query parameter when given", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ buckets: [] }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getInterestTimeline(12);

    // Assert
    const requestedUrl = fetchMock.mock.calls[0]?.[0] as string;
    expect(requestedUrl.endsWith("/api/interests/timeline?weeks=12")).toBe(true);
  }, TEST_TIMEOUT_MS);
});
