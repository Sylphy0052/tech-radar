import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CrawlRunPanel } from "@/components/features/CrawlRunPanel";
import { DEFAULT_MAX_CONSECUTIVE_ERRORS, DEFAULT_POLLING_INTERVAL_MS } from "@/hooks/usePolling";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function clickRunButton(): void {
  fireEvent.click(screen.getByRole("button", { name: "巡回を実行" }));
}

const jobId = "33333333-3333-3333-3333-333333333333";

function makeJob(status: string) {
  return {
    id: jobId,
    type: "crawl_sources",
    status,
    attempts: 1,
    available_at: "2026-08-01T00:00:00Z",
    created_at: "2026-08-01T00:00:00Z",
    started_at: "2026-08-01T00:00:01Z",
    finished_at: null,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("CrawlRunPanel", () => {
  it("disables the button while a crawl run is in progress", async () => {
    // Arrange
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ job_id: jobId, status: "pending" }, 201);
      }
      return jsonResponse(makeJob("searching"), 200);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);
    const button = screen.getByRole("button", { name: "巡回を実行" });
    expect(button).not.toBeDisabled();

    // Act
    clickRunButton();

    // Assert — POST がまだ解決していない間も無効化される。
    expect(button).toBeDisabled();

    await flush();

    // Assert — ジョブが実行中の間は無効のまま。
    expect(button).toBeDisabled();
  }, TEST_TIMEOUT_MS);

  it("shows the job progress while polling and re-enables the button once completed", async () => {
    // Arrange
    let getCallCount = 0;
    const statusesInOrder = ["pending", "searching", "completed"];
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ job_id: jobId, status: "pending" }, 201);
      }
      const status = statusesInOrder[getCallCount] ?? "completed";
      getCallCount += 1;
      return jsonResponse(makeJob(status), 200);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);

    // Act
    clickRunButton();
    await flush();
    await flush(DEFAULT_POLLING_INTERVAL_MS);
    await flush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert
    expect(screen.getByText("状態: 巡回完了")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "巡回を実行" })).not.toBeDisabled();
  }, TEST_TIMEOUT_MS);

  it("does not throw when the backend returns 200 for an already-running crawl", async () => {
    // Arrange — 進行中の巡回があれば 200 で既存ジョブを返す。
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ job_id: jobId, status: "searching" }, 200);
      }
      return jsonResponse(makeJob("searching"), 200);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);

    // Act
    clickRunButton();
    await flush();

    // Assert
    expect(screen.getByText("状態: 巡回実行中")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("recovers when the first progress request fails before the job becomes visible", async () => {
    // Arrange — 起動直後の 1 回だけ 404 が返り、その後は通常どおり進捗が返る。
    let getCallCount = 0;
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ job_id: jobId, status: "pending" }, 201);
      }
      getCallCount += 1;
      if (getCallCount === 1) {
        return jsonResponse({ detail: "ジョブが見つかりません" }, 404);
      }
      return jsonResponse(makeJob("searching"), 200);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);

    // Act
    clickRunButton();
    await flush();
    await flush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert
    expect(screen.getByText("状態: 巡回実行中")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("re-enables the button when polling the progress ultimately fails", async () => {
    // Arrange — 進捗取得が回復せず失敗し続ける。
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse({ job_id: jobId, status: "pending" }, 201);
      }
      return jsonResponse({ detail: "ジョブが見つかりません" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);

    // Act
    clickRunButton();
    await flush();
    await flush(DEFAULT_POLLING_INTERVAL_MS * DEFAULT_MAX_CONSECUTIVE_ERRORS);

    // Assert — 進捗が追えなくなった以上、押し直せる状態へ戻す。
    expect(screen.getByRole("button", { name: "巡回を実行" })).not.toBeDisabled();
  }, TEST_TIMEOUT_MS);

  it("resumes polling when the button is pressed again after the progress failed", async () => {
    // Arrange — 進捗取得が失敗し続けた後、再度押した以降は成功するようになる。
    let getCallCount = 0;
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        // 進行中の巡回があるため、押し直しても同じ job_id が返る。
        return jsonResponse({ job_id: jobId, status: "searching" }, 200);
      }
      getCallCount += 1;
      if (getCallCount <= DEFAULT_MAX_CONSECUTIVE_ERRORS) {
        return jsonResponse({ detail: "ジョブが見つかりません" }, 404);
      }
      return jsonResponse(makeJob("searching"), 200);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);
    clickRunButton();
    await flush();
    await flush(DEFAULT_POLLING_INTERVAL_MS * DEFAULT_MAX_CONSECUTIVE_ERRORS);

    // Act — 同じ job_id が返る状況でも押し直しでポーリングが再開すること
    clickRunButton();
    await flush();

    // Assert
    expect(screen.getByText("状態: 巡回実行中")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows an error message when starting the crawl fails with a 5xx response", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(new Response("boom", { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);

    // Act
    clickRunButton();
    await flush();

    // Assert
    expect(
      screen.getByText("サーバーでエラーが発生しました。しばらくしてから再度お試しください。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "巡回を実行" })).not.toBeDisabled();
  }, TEST_TIMEOUT_MS);

  it("shows an error message when starting the crawl fails with a network error", async () => {
    // Arrange
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    render(<CrawlRunPanel />);

    // Act
    clickRunButton();
    await flush();

    // Assert
    expect(
      screen.getByText("通信に失敗しました。しばらくしてから再度お試しください。"),
    ).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);
});
