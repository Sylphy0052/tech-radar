import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BulkArticleImportForm } from "@/components/features/BulkArticleImportForm";
import type { BulkArticleImportResult } from "@/lib/articles";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function makeFile(content = "https://example.com/a"): File {
  return new File([content], "urls.txt", { type: "text/plain" });
}

function selectFile(file: File): void {
  const input = screen.getByLabelText("URLリストファイル（.md / .txt）");
  fireEvent.change(input, { target: { files: [file] } });
}

function submitForm(): void {
  fireEvent.click(screen.getByRole("button", { name: "アップロードする" }));
}

const sampleResult: BulkArticleImportResult = {
  created: [],
  created_count: 3,
  duplicate_count: 1,
  error_count: 0,
  errors: [],
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BulkArticleImportForm", () => {
  it("sends the selected file as multipart form data to /api/articles/bulk", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleResult));
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);

    // Act
    selectFile(makeFile());
    await act(async () => {
      submitForm();
    });

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/articles/bulk");
    expect(init.body).toBeInstanceOf(FormData);
  }, TEST_TIMEOUT_MS);

  it("displays the created/duplicate/error counts after a successful upload", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleResult));
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);

    // Act
    selectFile(makeFile());
    await act(async () => {
      submitForm();
    });

    // Assert
    expect(screen.getByText("登録 3件 / 重複 1件 / エラー 0件")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("lists the line number, reason, and original line for each error row", async () => {
    // Arrange
    const resultWithErrors: BulkArticleImportResult = {
      created: [],
      created_count: 0,
      duplicate_count: 0,
      error_count: 1,
      errors: [{ line_number: 4, reason: "URLの形式が不正です", line: "not-a-url" }],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(resultWithErrors));
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);

    // Act
    selectFile(makeFile());
    await act(async () => {
      submitForm();
    });

    // Assert
    expect(screen.getByText("4行目: URLの形式が不正です（not-a-url）")).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows the payload-too-large message for a 413 response", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(new Response("too large", { status: 413 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);

    // Act
    selectFile(makeFile());
    await act(async () => {
      submitForm();
    });

    // Assert
    expect(
      screen.getByText(
        "ファイルが大きすぎるか、URLの件数が多すぎます（1MB以内、500件以内にしてください）",
      ),
    ).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("shows the unsupported-format message for a 422 response", async () => {
    // Arrange
    const fetchMock = vi.fn().mockResolvedValue(new Response("bad format", { status: 422 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);

    // Act
    selectFile(makeFile());
    await act(async () => {
      submitForm();
    });

    // Assert
    expect(
      screen.getByText(
        "対応していないファイル形式です（.md / .txt のUTF-8テキストのみ対応しています）",
      ),
    ).toBeInTheDocument();
  }, TEST_TIMEOUT_MS);

  it("does not send a second request when the button is clicked again before the first resolves", async () => {
    // Arrange
    let resolveFetch: (value: Response) => void = () => {};
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);
    selectFile(makeFile());

    // Act — 1 回目のクリックで送信中になった直後、応答未解決のまま 2 回目をクリックする
    fireEvent.click(screen.getByRole("button", { name: "アップロードする" }));
    fireEvent.click(screen.getByRole("button", { name: "アップロードする" }));

    // Assert
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // 後始末: 保留中の promise を解決してから終わる
    await act(async () => {
      resolveFetch(jsonResponse(sampleResult));
    });
  }, TEST_TIMEOUT_MS);

  it("does not call fetch and shows a validation message when no file is selected", () => {
    // Arrange
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<BulkArticleImportForm />);

    // Act
    submitForm();

    // Assert
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("ファイルを選択してください");
  }, TEST_TIMEOUT_MS);
});
