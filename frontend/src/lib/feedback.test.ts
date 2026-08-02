import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { BAD_REASON_LABELS, deleteFeedback, sendFeedback } from "@/lib/feedback";
import type { ArticleFeedback } from "@/lib/feedback";

afterEach(() => {
  vi.unstubAllGlobals();
});

const articleId = "11111111-1111-1111-1111-111111111111";

const samplePayload: ArticleFeedback = {
  action: "good",
  reason: null,
  created_at: "2026-08-01T00:00:00Z",
};

describe("sendFeedback", () => {
  it("posts action only when reason is omitted", async () => {
    // Arrange
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(samplePayload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await sendFeedback(articleId, { action: "good" });

    // Assert
    expect(result).toEqual(samplePayload);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`/api/articles/${articleId}/feedback`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ action: "good" });
  });

  it("includes the reason in the body when provided", async () => {
    // Arrange
    const payload: ArticleFeedback = {
      action: "bad",
      reason: "too_shallow",
      created_at: "2026-08-01T00:00:00Z",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await sendFeedback(articleId, { action: "bad", reason: "too_shallow" });

    // Assert
    expect(result).toEqual(payload);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ action: "bad", reason: "too_shallow" });
  });

  it("throws ApiError when the response fails", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("invalid", { status: 422 })),
    );

    // Act / Assert
    await expect(sendFeedback(articleId, { action: "good" })).rejects.toThrowError(ApiError);
  });
});

describe("deleteFeedback", () => {
  it("sends a DELETE request and resolves without a body on 204", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    // Act
    const result = await deleteFeedback(articleId);

    // Assert
    expect(result).toBeUndefined();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`/api/articles/${articleId}/feedback`);
    expect(init.method).toBe("DELETE");
  });

  it("throws ApiError when the response fails", async () => {
    // Arrange
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("invalid", { status: 422 })),
    );

    // Act / Assert
    await expect(deleteFeedback(articleId)).rejects.toThrowError(ApiError);
  });
});

describe("BAD_REASON_LABELS", () => {
  it("maps every BadReason to a Japanese label", () => {
    // Assert
    expect(BAD_REASON_LABELS).toEqual({
      not_interested: "テーマに興味がない",
      too_shallow: "内容が浅い",
      already_known: "既知の内容",
      promotional: "宣伝的",
      untrusted_source: "情報源を信頼できない",
      too_repetitive: "同じ内容を見すぎた",
    });
  });
});
