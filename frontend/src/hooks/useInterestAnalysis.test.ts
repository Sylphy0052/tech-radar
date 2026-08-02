import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useInterestAnalysis } from "@/hooks/useInterestAnalysis";

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

const emptySummary = {
  genres: [],
  feedback_ratio: { good_count: 0, bad_count: 0, save_count: 0 },
  technologies: [],
  primary_source_ratio: { primary_count: 0, secondary_count: 0 },
  content_types: [],
  difficulties: [],
  suppressed_topics: [],
};

function stubAllEndpoints(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockImplementation(async (url: string) => {
    if (url.includes("/api/interests/clusters")) {
      return jsonResponse({ items: [{ label: "AI", weight: 0.8, topics: ["llm"], updated_at: "2026-08-01T00:00:00Z" }] });
    }
    if (url.includes("/api/interests/timeline")) {
      return jsonResponse({ buckets: [] });
    }
    if (url.includes("/api/interests/summary")) {
      return jsonResponse(emptySummary);
    }
    throw new Error(`unexpected url: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("useInterestAnalysis", () => {
  it("starts in a loading state with no data", () => {
    // Arrange
    stubAllEndpoints();

    // Act
    const { result } = renderHook(() => useInterestAnalysis());

    // Assert
    expect(result.current.isLoading).toBe(true);
    expect(result.current.summary).toBeNull();
    expect(result.current.clusters).toBeNull();
    expect(result.current.timeline).toBeNull();
  });

  it("loads summary, clusters, and timeline together", async () => {
    // Arrange
    stubAllEndpoints();

    // Act
    const { result } = renderHook(() => useInterestAnalysis());

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.summary).toEqual(emptySummary);
    expect(result.current.clusters?.items).toHaveLength(1);
    expect(result.current.timeline).toEqual({ buckets: [] });
    expect(result.current.error).toBeNull();
  });

  it("surfaces an error message when any request fails", async () => {
    // Arrange
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    // Act
    const { result } = renderHook(() => useInterestAnalysis());

    // Assert
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.error).toBe("通信に失敗しました。しばらくしてから再度お試しください。");
    expect(result.current.summary).toBeNull();
  });
});
