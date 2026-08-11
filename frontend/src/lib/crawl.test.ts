import { afterEach, describe, expect, it, vi } from "vitest";

import { startCrawlRun } from "@/lib/crawl";
import type { CrawlRun } from "@/lib/crawl";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllGlobals();
});

const samplePayload: CrawlRun = {
  job_id: "22222222-2222-2222-2222-222222222222",
  status: "pending",
};

describe("startCrawlRun", () => {
  it("posts to /api/crawl/runs and returns the job", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await startCrawlRun();

    // Assert
    expect(result).toEqual(samplePayload);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/crawl/runs");
    expect(init.method).toBe("POST");
  }, TEST_TIMEOUT_MS);

  it("does not throw when a crawl run is already in progress (200 response)", async () => {
    // Arrange — 進行中の巡回があれば 200 で既存ジョブを返す。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await startCrawlRun();

    // Assert
    expect(result).toEqual(samplePayload);
  }, TEST_TIMEOUT_MS);
});
