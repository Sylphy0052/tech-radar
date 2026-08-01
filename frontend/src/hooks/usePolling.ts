"use client";

import { useEffect, useRef, useState } from "react";

/** ポーリング間隔（ミリ秒）。マジックナンバーを避けるため名前付き定数にする。 */
export const DEFAULT_POLLING_INTERVAL_MS = 2000;

interface PollingState<T> {
  data: T | null;
  error: Error | null;
  isLoading: boolean;
}

interface UsePollingOptions<T> {
  intervalMs?: number;
  /** true を返したら終端状態とみなし、ポーリングを止める。 */
  isTerminal: (data: T) => boolean;
}

/** どの id に対する結果かをあわせて保持する内部 state。 */
interface InternalState<T> {
  id: string | null;
  data: T | null;
  error: Error | null;
}

function toError(value: unknown): Error {
  return value instanceof Error ? value : new Error(String(value));
}

/**
 * `id` が非 null の間、`fetchFn(id)` を `intervalMs` 間隔で呼び出し続ける。
 *
 * - `id` が null の間は何もしない。
 * - `isTerminal` が true を返す状態に達したら以降のポーリングを止める。
 * - 取得中にエラーが起きた場合もポーリングを止める（無限にエラーを繰り返して
 *   バックグラウンドでリクエストを送り続けないため）。
 * - アンマウント時・`id` 変更時には必ずタイマーを解除し、リークさせない。
 *
 * `fetchFn` / `isTerminal` は毎レンダーで新しい関数参照が渡されてもポーリングが
 * 再起動しないよう ref 経由で参照する。ポーリングの再起動は `id`
 * （と `intervalMs`）の変化にのみ紐づける。
 *
 * state の更新は常に非同期コールバック（fetch の then/catch）内でのみ行い、
 * effect 本体で直接 setState しない。`id` が変わった直後（新しい結果が
 * 届く前）は、保持している state がまだ古い `id` のものだと判定し、
 * 取得中の見た目（data/error なし・isLoading true）へ写像することで
 * 「リセット」を表現する。
 */
export function usePolling<T>(
  id: string | null,
  fetchFn: (id: string) => Promise<T>,
  { intervalMs = DEFAULT_POLLING_INTERVAL_MS, isTerminal }: UsePollingOptions<T>,
): PollingState<T> {
  const [state, setState] = useState<InternalState<T>>({ id: null, data: null, error: null });

  const fetchFnRef = useRef(fetchFn);
  useEffect(() => {
    fetchFnRef.current = fetchFn;
  });

  const isTerminalRef = useRef(isTerminal);
  useEffect(() => {
    isTerminalRef.current = isTerminal;
  });

  useEffect(() => {
    if (id === null) {
      return;
    }

    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    const poll = (): void => {
      fetchFnRef
        .current(id)
        .then((data) => {
          if (cancelled) {
            return;
          }
          setState({ id, data, error: null });
          if (!isTerminalRef.current(data)) {
            timeoutId = setTimeout(poll, intervalMs);
          }
        })
        .catch((error: unknown) => {
          if (cancelled) {
            return;
          }
          setState({ id, data: null, error: toError(error) });
        });
    };

    poll();

    return () => {
      cancelled = true;
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
      }
    };
  }, [id, intervalMs]);

  const isCurrent = state.id === id;
  return {
    data: isCurrent ? state.data : null,
    error: isCurrent ? state.error : null,
    isLoading: id !== null && (!isCurrent || (state.data === null && state.error === null)),
  };
}
