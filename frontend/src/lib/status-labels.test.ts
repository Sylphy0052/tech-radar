import { describe, expect, it } from "vitest";

import {
  getJobStatusLabel,
  getRegistrationErrorMessage,
  getRegistrationStatusLabel,
  isTerminalStatus,
} from "@/lib/status-labels";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("getRegistrationStatusLabel", () => {
  it.each([
    ["pending", "登録待ち"],
    ["fetching", "記事を取得中"],
    ["analyzing", "記事を解析中"],
    ["searching", "関連情報を検索中"],
    ["completed", "登録完了"],
    ["failed", "登録失敗"],
  ])("maps %s to a Japanese label", (status, expected) => {
    // Act
    const label = getRegistrationStatusLabel(status);

    // Assert
    expect(label).toBe(expected);
  }, TEST_TIMEOUT_MS);

  it("falls back to a generic label for an unknown status", () => {
    // Arrange
    const status = "some_future_status";

    // Act
    const label = getRegistrationStatusLabel(status);

    // Assert
    expect(label).toBe("処理中");
  }, TEST_TIMEOUT_MS);
});

describe("getJobStatusLabel", () => {
  it.each([
    ["pending", "実行待ち"],
    ["searching", "巡回実行中"],
    ["completed", "巡回完了"],
    ["failed", "巡回失敗"],
  ])("maps %s to a Japanese label", (status, expected) => {
    // Act
    const label = getJobStatusLabel(status);

    // Assert
    expect(label).toBe(expected);
  }, TEST_TIMEOUT_MS);

  it("falls back to a generic label for an unknown status", () => {
    // Arrange
    const status = "some_future_status";

    // Act
    const label = getJobStatusLabel(status);

    // Assert
    expect(label).toBe("実行中");
  }, TEST_TIMEOUT_MS);
});

describe("isTerminalStatus", () => {
  it.each(["completed", "failed"])("treats %s as terminal", (status) => {
    // Act / Assert
    expect(isTerminalStatus(status)).toBe(true);
  }, TEST_TIMEOUT_MS);

  it.each(["pending", "fetching", "analyzing", "searching"])(
    "treats %s as non-terminal",
    (status) => {
      // Act / Assert
      expect(isTerminalStatus(status)).toBe(false);
    },
    TEST_TIMEOUT_MS,
  );
});

describe("getRegistrationErrorMessage", () => {
  it.each([
    ["fetch_failed", "記事の取得に失敗しました。URLを確認して再度お試しください。"],
    ["extraction_failed", "記事本文を取り出せませんでした。別のURLをお試しください。"],
    ["analysis_failed", "記事の解析に失敗しました。しばらくしてから再度お試しください。"],
    ["embedding_failed", "記事の登録処理に失敗しました。しばらくしてから再度お試しください。"],
  ])("maps %s to a user-facing Japanese message", (reason, expected) => {
    // Act
    const message = getRegistrationErrorMessage(reason);

    // Assert
    expect(message).toBe(expected);
  }, TEST_TIMEOUT_MS);

  it("falls back to a generic message for an unknown error_reason", () => {
    // Arrange
    const reason = "some_future_reason";

    // Act
    const message = getRegistrationErrorMessage(reason);

    // Assert
    expect(message).toBe("登録処理に失敗しました。しばらくしてから再度お試しください。");
  }, TEST_TIMEOUT_MS);

  it("falls back to a generic message when error_reason is null", () => {
    // Act
    const message = getRegistrationErrorMessage(null);

    // Assert
    expect(message).toBe("登録処理に失敗しました。しばらくしてから再度お試しください。");
  }, TEST_TIMEOUT_MS);
});
