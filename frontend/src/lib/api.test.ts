import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, getApiBaseUrl, getHealth } from "@/lib/api";
import type { Health } from "@/lib/api";

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
    expect(getApiBaseUrl()).toBe("http://localhost:8000");
  });

  it("strips trailing slashes from the configured base url", () => {
    // Arrange
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.example.com//";

    // Act / Assert
    expect(getApiBaseUrl()).toBe("https://api.example.com");
  });
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
  });

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
  });

  it("resolves to undefined on a 204 No Content response without parsing json", async () => {
    // Arrange — 204 はボディを持たないため、body に null を渡した Response で再現する
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));

    // Act
    const result = await apiFetch<void>("/api/articles/1/feedback", { method: "DELETE" });

    // Assert
    expect(result).toBeUndefined();
  });
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
  });

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
  });
});
