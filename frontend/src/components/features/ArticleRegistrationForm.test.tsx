import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArticleRegistrationForm } from "@/components/features/ArticleRegistrationForm";
import { DEFAULT_POLLING_INTERVAL_MS } from "@/hooks/usePolling";

async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function typeUrl(value: string): void {
  fireEvent.change(screen.getByLabelText("記事のURL"), { target: { value } });
}

function submitForm(): void {
  fireEvent.click(screen.getByRole("button", { name: "登録する" }));
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ArticleRegistrationForm", () => {
  it("rejects a url without a scheme before sending the request", () => {
    // Arrange
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("example.com/articles/1");
    submitForm();

    // Assert
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "httpまたはhttpsで始まるURLのみ登録できます",
    );
  });

  it("rejects an empty url before sending the request", () => {
    // Arrange
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    submitForm();

    // Assert
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("URLを入力してください");
  });

  it("registers a valid url and displays the returned status", async () => {
    // Arrange
    const registration = {
      id: "11111111-1111-1111-1111-111111111111",
      url: "https://example.com/a",
      status: "pending",
      article_id: null,
      error_reason: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(registration, 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();

    // Assert
    expect(fetchMock).toHaveBeenCalled();
    expect(screen.getByText("登録したURL: https://example.com/a")).toBeInTheDocument();
    expect(screen.getByText("状態: 登録待ち")).toBeInTheDocument();
  });

  it("does not break when the backend returns 200 for an already-registered url", async () => {
    // Arrange — 同じ URL の再登録は 200 で既存登録を返す。
    const registration = {
      id: "11111111-1111-1111-1111-111111111111",
      url: "https://example.com/a",
      status: "completed",
      article_id: "22222222-2222-2222-2222-222222222222",
      error_reason: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(registration, 200));
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();

    // Assert
    expect(screen.getByText("状態: 登録完了")).toBeInTheDocument();
  });

  it("updates the displayed status as polling progresses and stops once completed", async () => {
    // Arrange
    const registrationId = "11111111-1111-1111-1111-111111111111";
    const makeRegistration = (status: string) => ({
      id: registrationId,
      url: "https://example.com/a",
      status,
      article_id: null,
      error_reason: null,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    });
    let getCallCount = 0;
    const getStatusesInOrder = ["fetching", "analyzing", "completed"];
    const fetchMock = vi.fn().mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return jsonResponse(makeRegistration("pending"), 201);
      }
      const status = getStatusesInOrder[getCallCount] ?? "completed";
      getCallCount += 1;
      return jsonResponse(makeRegistration(status), 200);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();
    await flush(DEFAULT_POLLING_INTERVAL_MS);
    await flush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert
    expect(screen.getByText("状態: 登録完了")).toBeInTheDocument();
    const callCountAtCompletion = fetchMock.mock.calls.length;

    // ポーリングが止まっていること（さらに時間を進めても呼び出しが増えない）。
    await flush(DEFAULT_POLLING_INTERVAL_MS * 3);
    expect(fetchMock.mock.calls.length).toBe(callCountAtCompletion);
  });

  it("shows a Japanese error message for a known error_reason", async () => {
    // Arrange
    const registration = {
      id: "11111111-1111-1111-1111-111111111111",
      url: "https://example.com/a",
      status: "failed",
      article_id: null,
      error_reason: "fetch_failed",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(registration, 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();

    // Assert
    expect(
      screen.getByText("記事の取得に失敗しました。URLを確認して再度お試しください。"),
    ).toBeInTheDocument();
  });

  it("shows a generic error message for an unknown error_reason without throwing", async () => {
    // Arrange
    const registration = {
      id: "11111111-1111-1111-1111-111111111111",
      url: "https://example.com/a",
      status: "failed",
      article_id: null,
      error_reason: "some_future_reason",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(registration, 201));
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();

    // Assert
    expect(
      screen.getByText("登録処理に失敗しました。しばらくしてから再度お試しください。"),
    ).toBeInTheDocument();
  });

  it("shows an error message when the registration request fails with a 5xx response", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(new Response("boom", { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();

    // Assert
    expect(
      screen.getByText("サーバーでエラーが発生しました。しばらくしてから再度お試しください。"),
    ).toBeInTheDocument();
  });

  it("shows an error message when the registration request fails with a network error", async () => {
    // Arrange
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    render(<ArticleRegistrationForm />);

    // Act
    typeUrl("https://example.com/a");
    submitForm();
    await flush();

    // Assert
    expect(
      screen.getByText("通信に失敗しました。しばらくしてから再度お試しください。"),
    ).toBeInTheDocument();
  });
});
