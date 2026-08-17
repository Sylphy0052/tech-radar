import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_MAX_CONSECUTIVE_ERRORS,
  DEFAULT_POLLING_INTERVAL_MS,
  usePolling,
} from "@/hooks/usePolling";
import { ApiError } from "@/lib/api";
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

// Issue #102: 429（レート制限）は他のエラーと区別し、Retry-After の秒数だけ待って
// 再試行する。通信障害と違って「待てば回復する」とサーバー側が明示している状況の
// ため、consecutiveErrors も増やさない（3回で諦める挙動は通信障害向けのもの）。
describe("usePolling — 429 のレート制限応答", () => {
  it("waits for the Retry-After duration before retrying, instead of the default interval", async () => {
    // Arrange — Retry-After: 5秒のレート制限を1回返してから成功する
    const fetchFn = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(429, "rate limited", 5))
      .mockResolvedValueOnce({ status: "pending" } satisfies Item);

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    // 既定間隔（2秒）が経過しても Retry-After（5秒）にはまだ届いていない
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert — 既定間隔では再試行しない
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();

    // Act — Retry-After の残り秒数まで進める
    await advanceTimersAndFlush(5000 - DEFAULT_POLLING_INTERVAL_MS);

    // Assert — Retry-After の秒数が経過したところで再試行し、成功する
    expect(fetchFn).toHaveBeenCalledTimes(2);
    expect(result.current.data).toEqual({ status: "pending" });
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("falls back to the default interval when the 429 has no Retry-After", async () => {
    // Arrange — Retry-After ヘッダが無い（parseRetryAfterSeconds が null を返す）429
    const fetchFn = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(429, "rate limited", null))
      .mockResolvedValueOnce({ status: "pending" } satisfies Item);

    // Act
    renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert — 待ち時間不明のため既定間隔で再試行する
    expect(fetchFn).toHaveBeenCalledTimes(2);
  }, TEST_TIMEOUT_MS);

  it("falls back to the default interval when the Retry-After is zero", async () => {
    // Arrange — backend は待機秒数を整数へ切り上げるので、境界ちょうどで弾かれると
    // Retry-After: 0 になりうる。そのまま待ち時間へ使うと 0ms の即時再試行になる。
    const fetchFn = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(429, "rate limited", 0))
      .mockResolvedValueOnce({ status: "pending" } satisfies Item);

    // Act
    renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);

    // Assert — 0 秒は待ち時間として使わない（間を置かずに叩き直さない）
    expect(fetchFn).toHaveBeenCalledTimes(1);

    // Act — 既定間隔まで進める
    await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);

    // Assert — 既定間隔で再試行する
    expect(fetchFn).toHaveBeenCalledTimes(2);
  }, TEST_TIMEOUT_MS);

  it("keeps retrying through repeated 429s that carry no Retry-After", async () => {
    // Arrange — Retry-After 無しの429を上限を超える回数返し続ける。待ち時間が
    // 既定間隔へ倒れるぶん、429 分岐を通らず通常のエラー扱いになっていても
    // 2回目までは同じ見え方になる。連続失敗数に数えていないことは、上限を
    // 超えるまで回して初めて区別できる。
    const fetchFn = vi.fn().mockRejectedValue(new ApiError(429, "rate limited", null));

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    for (let i = 0; i < DEFAULT_MAX_CONSECUTIVE_ERRORS + 5; i += 1) {
      await advanceTimersAndFlush(DEFAULT_POLLING_INTERVAL_MS);
    }

    // Assert
    expect(fetchFn.mock.calls.length).toBeGreaterThan(DEFAULT_MAX_CONSECUTIVE_ERRORS);
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);

  it("keeps retrying through repeated 429s without ever surfacing an error", async () => {
    // Arrange — DEFAULT_MAX_CONSECUTIVE_ERRORS を超える回数、429 を返し続ける。
    // 通信障害なら3回連続でエラー確定するが、429 はサーバーが再開時刻を
    // 明示しているため、待てば回復するとみなして諦めない。
    const fetchFn = vi.fn().mockRejectedValue(new ApiError(429, "rate limited", 1));

    // Act
    const { result } = renderHook(() => usePolling<Item>("abc", fetchFn, { isTerminal }));
    await advanceTimersAndFlush(0);
    for (let i = 0; i < DEFAULT_MAX_CONSECUTIVE_ERRORS + 5; i += 1) {
      await advanceTimersAndFlush(1000);
    }

    // Assert — consecutiveErrors の上限を超えて呼ばれ続けても、エラーは確定しない
    expect(fetchFn.mock.calls.length).toBeGreaterThan(DEFAULT_MAX_CONSECUTIVE_ERRORS);
    expect(result.current.error).toBeNull();
  }, TEST_TIMEOUT_MS);
});
