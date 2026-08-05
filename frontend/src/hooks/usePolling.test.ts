import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_MAX_CONSECUTIVE_ERRORS,
  DEFAULT_POLLING_INTERVAL_MS,
  usePolling,
} from "@/hooks/usePolling";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

interface Item {
  status: string;
}

const isTerminal = (item: Item) => item.status === "done";

// フェイクタイマー進行でトリガーされる Promise 解決 → setState は React の
// バッチ更新の外で起きるため、act() で包んで反映を保証してから result.current を読む。
async function advanceTimersAndFlush(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("usePolling", () => {
  it("does not fetch while id is null", () => {
    // Arrange
    const fetchFn = vi.fn();

    // Act
    const { result } = renderHook(() => usePolling<Item>(null, fetchFn, { isTerminal }));

    // Assert
    expect(fetchFn).not.toHaveBeenCalled();
    expect(result.current).toEqual({ data: null, error: null, isLoading: false });
  }, TEST_TIMEOUT_MS);

  it("fetches immediately once an id is provided", async () => {
    // Arrange
    const fetchFn = vi.fn().mockResolvedValue({ status: "pending" } satisfies Item);

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);

    // Assert
    expect(fetchFn).toHaveBeenCalledExactlyOnceWith("abc");
    expect(result.current.data).toEqual({ status: "pending" });
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("polls again at the configured interval while non-terminal", async () => {
    // Arrange
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce({ status: "pending" } satisfies Item)
      .mockResolvedValueOnce({ status: "fetching" } satisfies Item);

    // Act
    renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert
    expect(fetchFn).toHaveBeenCalledTimes(2);
  }, TEST_TIMEOUT_MS);

  it("stops polling once a terminal status is reached", async () => {
    // Arrange
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce({ status: "pending" } satisfies Item)
      .mockResolvedValueOnce({ status: "done" } satisfies Item);

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS * 5);

    // Assert
    expect(fetchFn).toHaveBeenCalledTimes(2);
    expect(result.current.data).toEqual({ status: "done" });
  }, TEST_TIMEOUT_MS);

  it("stops polling on unmount so no further requests leak", async () => {
    // Arrange
    const fetchFn = vi.fn().mockResolvedValue({ status: "pending" } satisfies Item);

    // Act
    const { unmount } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    unmount();
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS * 5);

    // Assert
    expect(fetchFn).toHaveBeenCalledTimes(1);
  }, TEST_TIMEOUT_MS);

  it("keeps polling after a transient failure instead of giving up", async () => {
    // Arrange — 起動直後の 404 のように、しばらくすると解消する失敗を1回だけ返す
    const fetchFn = vi
      .fn()
      .mockRejectedValueOnce(new Error("not found"))
      .mockResolvedValueOnce({ status: "pending" } satisfies Item);

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    const afterFirstFailure = { ...result.current };
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert — 1回の失敗ではエラーを確定させず、回復したら結果を返す
    expect(afterFirstFailure.error).toBeNull();
    expect(result.current.data).toEqual({ status: "pending" });
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("surfaces a fetch error once the failures no longer look transient", async () => {
    // Arrange
    const fetchFn = vi.fn().mockRejectedValue(new Error("network down"));

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS * DEFAULT_MAX_CONSECUTIVE_ERRORS);

    // Assert
    expect(fetchFn).toHaveBeenCalledTimes(DEFAULT_MAX_CONSECUTIVE_ERRORS);
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.data).toBeNull();
    expect(result.current.isLoading).toBe(false);
  }, TEST_TIMEOUT_MS);

  it("stops retrying once the failure is surfaced", async () => {
    // Arrange
    const fetchFn = vi.fn().mockRejectedValue(new Error("network down"));

    // Act
    renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS * (DEFAULT_MAX_CONSECUTIVE_ERRORS + 5));

    // Assert
    expect(fetchFn).toHaveBeenCalledTimes(DEFAULT_MAX_CONSECUTIVE_ERRORS);
  }, TEST_TIMEOUT_MS);

  it("forgets earlier failures once a fetch succeeds", async () => {
    // Arrange — 失敗を挟みながらも成功が入るうちはエラーを確定させない
    const fetchFn = vi
      .fn()
      .mockRejectedValueOnce(new Error("not found"))
      .mockRejectedValueOnce(new Error("not found"))
      .mockResolvedValueOnce({ status: "pending" } satisfies Item)
      .mockRejectedValueOnce(new Error("not found"))
      .mockRejectedValueOnce(new Error("not found"))
      .mockResolvedValueOnce({ status: "done" } satisfies Item);

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS * 5);

    // Assert
    expect(result.current.data).toEqual({ status: "done" });
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("restarts polling from scratch when the id changes", async () => {
    // Arrange
    const fetchFn = vi.fn().mockResolvedValue({ status: "pending" } satisfies Item);

    // Act
    const { rerender } = renderHook(({ id }) => usePolling<Item>(id, fetchFn, { isTerminal }), {
      initialProps: { id: "abc" },
    });
    await advanceTimersAndFlush(0);
    rerender({ id: "def" });
    await advanceTimersAndFlush(0);

    // Assert
    expect(fetchFn).toHaveBeenNthCalledWith(1, "abc");
    expect(fetchFn).toHaveBeenNthCalledWith(2, "def");
  }, TEST_TIMEOUT_MS);
});
