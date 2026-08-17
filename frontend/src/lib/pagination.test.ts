import { describe, expect, it } from "vitest";

import { FIRST_PAGE, MAX_PAGE, parsePageOrFirst } from "@/lib/pagination";
import { TEST_TIMEOUT_MS } from "@/test-utils/timeouts";

describe("parsePageOrFirst", () => {
  it("falls back to the first page when the query is absent", () => {
    expect(parsePageOrFirst(null)).toBe(FIRST_PAGE);
  }, TEST_TIMEOUT_MS);

  it("reads a valid page number", () => {
    expect(parsePageOrFirst("3")).toBe(3);
  }, TEST_TIMEOUT_MS);

  it("accepts the upper bound shared with the backend", () => {
    expect(parsePageOrFirst(String(MAX_PAGE))).toBe(MAX_PAGE);
  }, TEST_TIMEOUT_MS);

  // 壊れた URL をそのまま `GET /api/feed` や `GET /api/articles` へ送ると 422 になる。
  // 共有リンク・履歴・手動編集でクエリは容易に壊れるため、1ページ目へ落として
  // 表示だけは成立させる（`parseMaxAgeDaysOrNull` と同じ狙い）。
  it.each([
    ["a negative page", "-1"],
    ["zero", "0"],
    ["a fractional page", "1.5"],
    ["a non-numeric page", "abc"],
    ["an empty string", ""],
    ["a page above the backend upper bound", String(MAX_PAGE + 1)],
  ])("falls back to the first page for %s", (_label, value) => {
    expect(parsePageOrFirst(value)).toBe(FIRST_PAGE);
  }, TEST_TIMEOUT_MS);

  // `Number()` は10進の整数リテラル以外も数値へ変換する。これらはページ番号として
  // 打ち込まれたものではなく、壊れた URL とみなして1ページ目へ落とす。素通しすると
  // `page=1e2` のような URL が backend へそのまま渡り、ページャの表示と URL が
  // 食い違ったまま100ページ目が出る。
  it.each([
    ["exponent notation", "1e2"],
    ["hexadecimal notation", "0x10"],
    ["a leading plus sign", "+3"],
    ["surrounding whitespace", " 3 "],
    ["leading zeros", "007"],
    ["a numeric separator", "1_000"],
  ])("falls back to the first page for %s", (_label, value) => {
    expect(parsePageOrFirst(value)).toBe(FIRST_PAGE);
  }, TEST_TIMEOUT_MS);
});

describe("MAX_PAGE", () => {
  // backend の `MAX_PAGE_NUMBER` を写経した値なので、片方だけ変えるとずれる。ずれても
  // 型チェックも lint も通り、気付くのは実際に 422 が出たときになる。openapi.json は
  // backend の実装から生成しているため、そこに出た上限と突き合わせれば機械で押さえられる
  // （`check.sh` の「openapi.jsonの鮮度」が openapi.json 自体の古さを見ているので、
  // 生成が古いまま通り抜けることもない）。
  //
  // `/api/feed` と `/api/articles` の両方を見る。backend はこの2つのエンドポイントを
  // 単一の `MAX_PAGE_NUMBER` で制約しており（`api/query_filters.py`）、フロント側も
  // ここへ集約したため、突き合わせも1箇所にまとめる（Issue #100）。
  it.each(["/api/feed", "/api/articles"])(
    "matches the upper bound the backend advertises for GET %s",
    async (path) => {
      // Arrange
      const { readFile } = await import("node:fs/promises");
      const { resolve } = await import("node:path");
      // `import.meta.url` は vite の変換後に file: スキームでなくなるため使えない。
      // vitest は `frontend/` から起動する（`package.json` の `test` を `check.sh` が
      // その位置で叩く）ので、そこからの相対で解決する。
      const openapi = JSON.parse(
        await readFile(resolve(process.cwd(), "../backend/openapi.json"), "utf-8"),
      ) as {
        paths: Record<string, { get: { parameters: { name: string; schema: { maximum?: number } }[] } }>;
      };

      // Act
      const pageParameter = openapi.paths[path]?.get.parameters.find(
        (parameter) => parameter.name === "page",
      );

      // Assert
      expect(pageParameter?.schema.maximum).toBe(MAX_PAGE);
    },
    TEST_TIMEOUT_MS,
  );
});
