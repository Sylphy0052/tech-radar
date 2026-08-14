import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import {
  buildSearchParamsFromFilters,
  EMPTY_FEED_FILTERS,
  FIRST_FEED_PAGE,
  getFeed,
  isoToJstDateInputValue,
  jstDateToPublishedFromIso,
  jstDateToPublishedToIso,
  MAX_FEED_PAGE,
  parseFeedFiltersFromSearchParams,
  parseFeedPageOrFirst,
} from "@/lib/feed";
import type { FeedFilters, FeedResponse } from "@/lib/feed";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

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
  total_count: 1,
  page: 1,
  page_size: 20,
  total_pages: 1,
};

describe("parseFeedFiltersFromSearchParams", () => {
  it("returns empty filters when the search params are empty", () => {
    // Arrange
    const searchParams = new URLSearchParams();

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters).toEqual(EMPTY_FEED_FILTERS);
  }, TEST_TIMEOUT_MS);

  it("reads the search term", () => {
    // Arrange
    const searchParams = new URLSearchParams("q=rust");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.q).toBe("rust");
  }, TEST_TIMEOUT_MS);

  it("reads multiple topics values", () => {
    // Arrange
    const searchParams = new URLSearchParams("topics=ai&topics=web");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.topics).toEqual(["ai", "web"]);
  }, TEST_TIMEOUT_MS);

  it("reads multiple technologies values", () => {
    // Arrange
    const searchParams = new URLSearchParams("technologies=rust&technologies=wasm");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.technologies).toEqual(["rust", "wasm"]);
  }, TEST_TIMEOUT_MS);

  it("reads source_domain", () => {
    // Arrange
    const searchParams = new URLSearchParams("source_domain=blog.example.com");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.sourceDomain).toBe("blog.example.com");
  }, TEST_TIMEOUT_MS);

  it("reads a valid published_from/published_to date range", () => {
    // Arrange
    const searchParams = new URLSearchParams(
      "published_from=2026-07-31T15:00:00.000Z&published_to=2026-08-01T14:59:59.999Z",
    );

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.publishedFrom).toBe("2026-07-31T15:00:00.000Z");
    expect(filters.publishedTo).toBe("2026-08-01T14:59:59.999Z");
  }, TEST_TIMEOUT_MS);

  it("reads the feed window in days", () => {
    // Arrange
    const searchParams = new URLSearchParams("max_age_days=30");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.maxAgeDays).toBe(30);
  }, TEST_TIMEOUT_MS);

  it("drops an out-of-range feed window instead of forwarding it to the API", () => {
    // Arrange — 上限（180日）超えは 422 になるため、指定なしへ落として既定に任せる
    const searchParams = new URLSearchParams("max_age_days=999");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.maxAgeDays).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("drops a non-integer feed window instead of forwarding it to the API", () => {
    // Arrange — 壊れた URL クエリ（手編集・共有リンク）を素通しさせない
    const searchParams = new URLSearchParams("max_age_days=abc");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.maxAgeDays).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("drops an unparsable published_from value instead of forwarding it to the API", () => {
    // Arrange — 手動編集や壊れた共有リンクを想定
    const searchParams = new URLSearchParams("published_from=not-a-date");

    // Act
    const filters = parseFeedFiltersFromSearchParams(searchParams);

    // Assert
    expect(filters.publishedFrom).toBeNull();
  }, TEST_TIMEOUT_MS);
});

describe("parseFeedPageOrFirst", () => {
  it("falls back to the first page when the query is absent", () => {
    expect(parseFeedPageOrFirst(null)).toBe(FIRST_FEED_PAGE);
  }, TEST_TIMEOUT_MS);

  it("reads a valid page number", () => {
    expect(parseFeedPageOrFirst("3")).toBe(3);
  }, TEST_TIMEOUT_MS);

  it("accepts the upper bound shared with the backend", () => {
    expect(parseFeedPageOrFirst(String(MAX_FEED_PAGE))).toBe(MAX_FEED_PAGE);
  }, TEST_TIMEOUT_MS);

  // 壊れた URL をそのまま `GET /api/feed` へ送ると 422 になる。共有リンク・履歴・
  // 手動編集でクエリは容易に壊れるため、1ページ目へ落として表示だけは成立させる
  // （`parseMaxAgeDaysOrNull` と同じ狙い）。
  it.each([
    ["a negative page", "-1"],
    ["zero", "0"],
    ["a fractional page", "1.5"],
    ["a non-numeric page", "abc"],
    ["an empty string", ""],
    ["a page above the backend upper bound", String(MAX_FEED_PAGE + 1)],
  ])("falls back to the first page for %s", (_label, value) => {
    expect(parseFeedPageOrFirst(value)).toBe(FIRST_FEED_PAGE);
  }, TEST_TIMEOUT_MS);
});

describe("MAX_FEED_PAGE", () => {
  // backend の `MAX_PAGE_NUMBER` を写経した値なので、片方だけ変えるとずれる。ずれても
  // 型チェックも lint も通り、気付くのは実際に 422 が出たときになる。openapi.json は
  // backend の実装から生成しているため、そこに出た上限と突き合わせれば機械で押さえられる
  // （`check.sh` の「openapi.jsonの鮮度」が openapi.json 自体の古さを見ているので、
  // 生成が古いまま通り抜けることもない）。
  it("matches the upper bound the backend advertises for GET /api/feed", async () => {
    // Arrange
    const { readFile } = await import("node:fs/promises");
    const { resolve } = await import("node:path");
    // `import.meta.url` は vite の変換後に file: スキームでなくなるため使えない。
    // vitest は `frontend/` から起動する（`package.json` の `test` を `check.sh` が
    // その位置で叩く）ので、そこからの相対で解決する。
    const openapi = JSON.parse(
      await readFile(resolve(process.cwd(), "../backend/openapi.json"), "utf-8"),
    ) as {
      paths: Record<string, { get: { parameters: { name: string; schema: { maximum?: number } }[] } }>;
    };

    // Act
    const pageParameter = openapi.paths["/api/feed"]?.get.parameters.find(
      (parameter) => parameter.name === "page",
    );

    // Assert
    expect(pageParameter?.schema.maximum).toBe(MAX_FEED_PAGE);
  }, TEST_TIMEOUT_MS);
});

describe("buildSearchParamsFromFilters", () => {
  it("returns empty params for empty filters", () => {
    // Act
    const params = buildSearchParamsFromFilters(EMPTY_FEED_FILTERS);

    // Assert
    expect(params.toString()).toBe("");
  }, TEST_TIMEOUT_MS);

  it("round-trips combined filters through parseFeedFiltersFromSearchParams", () => {
    // Arrange
    const filters: FeedFilters = {
      q: "rust",
      topics: ["ai", "web"],
      technologies: ["rust", "wasm"],
      publishedFrom: "2026-07-31T15:00:00.000Z",
      publishedTo: "2026-08-01T14:59:59.999Z",
      sourceDomain: "blog.example.com",
      maxAgeDays: 30,
    };

    // Act
    const params = buildSearchParamsFromFilters(filters);
    const roundTripped = parseFeedFiltersFromSearchParams(params);

    // Assert
    expect(roundTripped).toEqual(filters);
  }, TEST_TIMEOUT_MS);
});

describe("jstDateToPublishedFromIso / jstDateToPublishedToIso / isoToJstDateInputValue", () => {
  it("converts a date-only string to the start of the JST day in UTC", () => {
    expect(jstDateToPublishedFromIso("2026-08-01")).toBe("2026-07-31T15:00:00.000Z");
  }, TEST_TIMEOUT_MS);

  it("converts a date-only string to the end of the JST day in UTC", () => {
    expect(jstDateToPublishedToIso("2026-08-01")).toBe("2026-08-01T14:59:59.999Z");
  }, TEST_TIMEOUT_MS);

  it("converts a UTC ISO string back to the JST calendar date", () => {
    expect(isoToJstDateInputValue("2026-07-31T15:00:00.000Z")).toBe("2026-08-01");
  }, TEST_TIMEOUT_MS);

  it("returns an empty string instead of throwing for an unparsable ISO string", () => {
    expect(() => isoToJstDateInputValue("not-a-date")).not.toThrow();
    expect(isoToJstDateInputValue("not-a-date")).toBe("");
  }, TEST_TIMEOUT_MS);
});

describe("getFeed", () => {
  it("requests /api/feed without query params when filters are empty and no options given", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(samplePayload));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await getFeed(EMPTY_FEED_FILTERS);

    // Assert
    expect(result).toEqual(samplePayload);
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("/api/feed");
    expect(url.endsWith("/api/feed")).toBe(true);
  }, TEST_TIMEOUT_MS);

  it("includes the search term and array filters in the query string", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(samplePayload));
    vi.stubGlobal("fetch", fetchMock);
    const filters: FeedFilters = {
      q: "rust",
      topics: ["ai", "web"],
      technologies: ["rust"],
      publishedFrom: null,
      publishedTo: null,
      sourceDomain: "blog.example.com",
      maxAgeDays: 30,
    };

    // Act
    await getFeed(filters);

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    const query = new URL(url).searchParams;
    expect(query.get("q")).toBe("rust");
    expect(query.getAll("topics")).toEqual(["ai", "web"]);
    expect(query.getAll("technologies")).toEqual(["rust"]);
    expect(query.get("source_domain")).toBe("blog.example.com");
    expect(query.get("max_age_days")).toBe("30");
  }, TEST_TIMEOUT_MS);

  it("includes the page in the query string when provided", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(samplePayload));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getFeed(EMPTY_FEED_FILTERS, { page: 3 });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("page=3");
  }, TEST_TIMEOUT_MS);

  it("includes the limit in the query string when provided", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(samplePayload));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getFeed(EMPTY_FEED_FILTERS, { limit: 20 });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("limit=20");
  }, TEST_TIMEOUT_MS);

  it("includes both page and limit when both are provided", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(samplePayload));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await getFeed(EMPTY_FEED_FILTERS, { page: 2, limit: 20 });

    // Assert
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toContain("page=2");
    expect(url).toContain("limit=20");
  }, TEST_TIMEOUT_MS);

  it("throws ApiError when the response fails", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("bad request", { status: 400 })),
    );

    // Act / Assert
    await expect(getFeed(EMPTY_FEED_FILTERS)).rejects.toThrowError(ApiError);
  }, TEST_TIMEOUT_MS);
});
