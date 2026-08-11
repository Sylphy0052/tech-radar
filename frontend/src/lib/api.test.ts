import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, getApiBaseUrl, getHealth, isRateLimitError } from "@/lib/api";
import type { Health } from "@/lib/api";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

const ORIGINAL_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL;

afterEach(() => {
  process.env.NEXT_PUBLIC_API_BASE_URL = ORIGINAL_BASE_URL;
  vi.unstubAllGlobals();
});

describe("getApiBaseUrl", () => {
  it("falls back to localhost when the env var is unset", () => {
    // Arrange
    delete process.env.NEXT_PUBLIC_API_BASE_URL;

    // Act / Assert
    expect(getApiBaseUrl()).toBe("http://localhost:18700");
  }, TEST_TIMEOUT_MS);

  it("strips trailing slashes from the configured base url", () => {
    // Arrange
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.example.com//";

    // Act / Assert
    expect(getApiBaseUrl()).toBe("https://api.example.com");
  }, TEST_TIMEOUT_MS);
});

describe("apiFetch", () => {
  it("returns the parsed json body on success", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await apiFetch<{ status: string }>("/api/health");

    // Assert
    expect(result).toEqual({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledOnce();
  }, TEST_TIMEOUT_MS);

  it("throws ApiError carrying the status code when the response fails", async () => {
    // Arrange — Response のボディは 1 度しか読めないため呼び出しごとに生成する
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response("not found", { status: 404 })),
    );

    // Act / Assert
    await expect(apiFetch("/api/missing")).rejects.toThrowError(ApiError);
    await expect(apiFetch("/api/missing")).rejects.toMatchObject({
      status: 404,
      message: "not found",
    });
  }, TEST_TIMEOUT_MS);

  it("carries the Retry-After seconds on a 429 response", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        async () =>
          new Response("too many requests", { status: 429, headers: { "Retry-After": "30" } }),
      ),
    );

    // Act / Assert
    await expect(apiFetch("/api/feed")).rejects.toMatchObject({
      status: 429,
      retryAfterSeconds: 30,
    });
  }, TEST_TIMEOUT_MS);

  it("leaves retryAfterSeconds null when the Retry-After header is absent", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => new Response("too many requests", { status: 429 })),
    );

    // Act / Assert
    await expect(apiFetch("/api/feed")).rejects.toMatchObject({
      status: 429,
      retryAfterSeconds: null,
    });
  }, TEST_TIMEOUT_MS);

  it.each([
    ["a negative value", "-1"],
    ["a hexadecimal notation", "0x1e"],
    ["an exponential notation", "1e2"],
    ["a value just beyond the accepted upper bound", "301"],
    ["a value far beyond the accepted upper bound", "999999999"],
  ])("leaves retryAfterSeconds null for %s in Retry-After", async (_label, headerValue) => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        async () =>
          new Response("too many requests", {
            status: 429,
            headers: { "Retry-After": headerValue },
          }),
      ),
    );

    // Act / Assert
    await expect(apiFetch("/api/feed")).rejects.toMatchObject({ retryAfterSeconds: null });
  }, TEST_TIMEOUT_MS);

  it("keeps a Retry-After value exactly at the accepted upper bound", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        async () =>
          new Response("too many requests", { status: 429, headers: { "Retry-After": "300" } }),
      ),
    );

    // Act / Assert
    await expect(apiFetch("/api/feed")).rejects.toMatchObject({ retryAfterSeconds: 300 });
  }, TEST_TIMEOUT_MS);

  it("leaves retryAfterSeconds null when the Retry-After header is not delta-seconds", async () => {
    // Arrange — HTTP-date 形式（backend は返さないが、規格上は許容される）
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        async () =>
          new Response("too many requests", {
            status: 429,
            headers: { "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT" },
          }),
      ),
    );

    // Act / Assert
    await expect(apiFetch("/api/feed")).rejects.toMatchObject({ retryAfterSeconds: null });
  }, TEST_TIMEOUT_MS);

  it("resolves to undefined on a 204 No Content response without parsing json", async () => {
    // Arrange — 204 はボディを持たないため、body に null を渡した Response で再現する
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));

    // Act
    const result = await apiFetch<void>("/api/articles/1/feedback", { method: "DELETE" });

    // Assert
    expect(result).toBeUndefined();
  }, TEST_TIMEOUT_MS);

  it("omits the default Content-Type header when the body is FormData", async () => {
    // Arrange — FormData を渡す一括インポートでは、ブラウザに boundary 付きの
    // Content-Type を自動設定させる必要があるため、既定の application/json を付けない。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const formData = new FormData();
    formData.append("file", new File(["a"], "urls.txt", { type: "text/plain" }));

    // Act
    await apiFetch("/api/articles/bulk", { method: "POST", body: formData });

    // Assert
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  }, TEST_TIMEOUT_MS);

  it("keeps an explicitly provided Content-Type header even when the body is FormData", async () => {
    // Arrange — 既存の「明示的な headers を優先する」方針は FormData でも変わらない。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const formData = new FormData();

    // Act
    await apiFetch("/api/articles/bulk", {
      method: "POST",
      body: formData,
      headers: { "Content-Type": "multipart/form-data; boundary=custom" },
    });

    // Assert
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "multipart/form-data; boundary=custom",
    );
  }, TEST_TIMEOUT_MS);

  it("still sends the default Content-Type header for a non-FormData body", async () => {
    // Arrange — 既存の JSON 送信（registerArticle 等）の挙動を壊していないことの確認。
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    await apiFetch("/api/health", { method: "GET" });

    // Assert
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  }, TEST_TIMEOUT_MS);
});

describe("isRateLimitError", () => {
  it("identifies only a 429 ApiError as a rate limit error", () => {
    // Act / Assert
    expect(isRateLimitError(new ApiError(429, "too many requests"))).toBe(true);
    expect(isRateLimitError(new ApiError(404, "not found"))).toBe(false);
    expect(isRateLimitError(new TypeError("Failed to fetch"))).toBe(false);
  }, TEST_TIMEOUT_MS);
});

describe("getHealth", () => {
  it("requests the health endpoint", async () => {
    // Arrange
    const payload = { status: "ok", version: "0.1.0", brave_search_enabled: false };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await getHealth();

    // Assert
    expect(result).toEqual(payload);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/health");
  }, TEST_TIMEOUT_MS);

  it("resolves to a value assignable to the generated HealthResponse schema type", async () => {
    // Arrange — `satisfies` により、生成型 (openapi-typescript) からずれると
    // `tsc --noEmit` が失敗する。手書きの Health 型を生成型から導出済みであることの検証。
    const payload = {
      status: "ok",
      version: "0.1.0",
      brave_search_enabled: false,
    } satisfies Health;
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result: Health = await getHealth();

    // Assert
    expect(result).toEqual(payload);
  }, TEST_TIMEOUT_MS);
});
