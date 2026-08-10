import { afterEach, describe, expect, it, vi } from "vitest";

import { bulkImportArticles, getArticleRegistration, registerArticle } from "@/lib/articles";
import type { ArticleRegistration, BulkArticleImportResult } from "@/lib/articles";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

afterEach(() => {
  vi.unstubAllGlobals();
});

const samplePayload: ArticleRegistration = {
  id: "11111111-1111-1111-1111-111111111111",
  url: "https://example.com/a",
  status: "pending",
  article_id: null,
  error_reason: null,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
};

describe("registerArticle", () => {
  it("posts the url to /api/articles and returns the registration", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await registerArticle("https://example.com/a");

    // Assert
    expect(result).toEqual(samplePayload);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/articles");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ url: "https://example.com/a" });
  }, TEST_TIMEOUT_MS);

  it("does not throw when the backend returns 200 for an already-registered url", async () => {
    // Arrange — 同一 URL の再登録は 200 で既存登録を返す。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await registerArticle("https://example.com/a");

    // Assert
    expect(result).toEqual(samplePayload);
  }, TEST_TIMEOUT_MS);
});

describe("getArticleRegistration", () => {
  it("requests the registration status by id", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await getArticleRegistration(samplePayload.id);

    // Assert
    expect(result).toEqual(samplePayload);
    expect(fetchMock.mock.calls[0][0]).toContain(
      `/api/articles/registrations/${samplePayload.id}`,
    );
  }, TEST_TIMEOUT_MS);
});

describe("bulkImportArticles", () => {
  const sampleResult: BulkArticleImportResult = {
    created: [samplePayload],
    created_count: 1,
    duplicate_count: 0,
    error_count: 0,
    errors: [],
  };

  it("posts the file to /api/articles/bulk as multipart form data", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(sampleResult), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["https://example.com/a"], "urls.txt", { type: "text/plain" });

    // Act
    const result = await bulkImportArticles(file);

    // Assert
    expect(result).toEqual(sampleResult);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/articles/bulk");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const formData = init.body as FormData;
    expect(formData.get("file")).toBe(file);
  }, TEST_TIMEOUT_MS);
});
