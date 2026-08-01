import { afterEach, describe, expect, it, vi } from "vitest";

import { getArticleRegistration, registerArticle } from "@/lib/articles";
import type { ArticleRegistration } from "@/lib/articles";

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
  });

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
  });
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
  });
});
