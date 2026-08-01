import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { getFeed } from "@/lib/feed";
import type { FeedResponse } from "@/lib/feed";

afterEach(() => {
  vi.unstubAllGlobals();
});

const sampleItem: FeedResponse["items"][number] = {
  article_id: "11111111-1111-1111-1111-111111111111",
  canonical_url: "https://example.com/a",
  original_url: "https://example.com/a",
  feedback: null,
  is_primary_source: true,
  is_read: false,
  language: "en",
  published_at: "2026-08-01T00:00:00Z",
  rank: 1,
  reasons: {},
  score: 0.9,
  source_domain: "example.com",
  summary_ja: "要約",
  technologies: [],
  title: "Title",
  topics: [],
  translated_title: null,
};

const samplePayload: FeedResponse = {
  items: [sampleItem],
  next_cursor: "cursor-1",
};

describe("getFeed", () => {
  it("requests /api/feed without query params when called with no arguments", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await getFeed();

    // Assert
    expect(result).toEqual(samplePayload);
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/api/feed");
    expect(url).not.toContain("cursor=");
    expect(url).not.toContain("limit=");
  });

  it("includes the cursor in the query string when provided", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getFeed({ cursor: "abc-cursor" });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/api/feed?cursor=abc-cursor");
  });

  it("includes the limit in the query string when provided", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getFeed({ limit: 20 });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("limit=20");
  });

  it("includes both cursor and limit when both are provided", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getFeed({ cursor: "abc-cursor", limit: 20 });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("cursor=abc-cursor");
    expect(url).toContain("limit=20");
  });

  it("throws ApiError when the response fails", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("bad request", { status: 400 })),
    );

    // Act / Assert
    await expect(getFeed()).rejects.toThrowError(ApiError);
  });
});
