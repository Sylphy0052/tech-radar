import { afterEach, describe, expect, it, vi } from "vitest";

import { getJob } from "@/lib/jobs";
import type { Job } from "@/lib/jobs";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllGlobals();
});

const samplePayload: Job = {
  id: "33333333-3333-3333-3333-333333333333",
  type: "crawl_sources",
  status: "searching",
  attempts: 1,
  available_at: "2026-08-01T00:00:00Z",
  created_at: "2026-08-01T00:00:00Z",
  started_at: "2026-08-01T00:00:01Z",
  finished_at: null,
};

describe("getJob", () => {
  it("requests the job progress by id", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await getJob(samplePayload.id);

    // Assert
    expect(result).toEqual(samplePayload);
    expect(fetchMock.mock.calls[0][0]).toContain(`/api/jobs/${samplePayload.id}`);
  }, TEST_TIMEOUT_MS);
});
