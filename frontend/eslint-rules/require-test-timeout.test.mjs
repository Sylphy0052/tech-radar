import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import requireTestTimeout from "./require-test-timeout.mjs";

// RuleTester は describe / it をグローバルから探す。vitest は `globals: true` でも
// ファイル内の import を優先するため、明示的に渡して取り違えを防ぐ。
RuleTester.describe = describe;
RuleTester.it = it;

const ruleTester = new RuleTester({
  languageOptions: { ecmaVersion: 2022, sourceType: "module" },
});

ruleTester.run("require-test-timeout", requireTestTimeout, {
  valid: [
    {
      name: "it に TEST_TIMEOUT_MS が渡っている",
      code: `it("does something", () => {}, TEST_TIMEOUT_MS);`,
    },
    {
      name: "test に TEST_TIMEOUT_MS が渡っている",
      code: `test("does something", () => {}, TEST_TIMEOUT_MS);`,
    },
    {
      name: "it.only / it.skip にも渡っている",
      code: `
        it.only("does something", () => {}, TEST_TIMEOUT_MS);
        it.skip("does something else", () => {}, TEST_TIMEOUT_MS);
      `,
    },
    {
      name: "it.concurrent にも渡っている",
      code: `it.concurrent("does something", () => {}, TEST_TIMEOUT_MS);`,
    },
    {
      name: "it.each の呼び出し結果に渡っている",
      code: `it.each([1, 2])("handles %s", (n) => {}, TEST_TIMEOUT_MS);`,
    },
    {
      name: "it.skip.each の呼び出し結果に渡っている",
      code: `it.skip.each([1, 2])("handles %s", (n) => {}, TEST_TIMEOUT_MS);`,
    },
    {
      name: "タグ付きテンプレートの it.each にも渡っている",
      code: "it.each`a`(\"handles $a\", ({ a }) => {}, TEST_TIMEOUT_MS);",
    },
    {
      // `it.for` は `(name, fn, timeout)` を取らず、オプション形式だけを取る。
      name: "it.each / it.for はオプション形式でも渡せる",
      code: `
        it.for([1, 2])("handles %s", { timeout: TEST_TIMEOUT_MS }, (n) => {});
        it.each([1, 2])("handles %s", { timeout: TEST_TIMEOUT_MS }, (n) => {});
      `,
    },
    {
      name: "it 本体もオプション形式で渡せる",
      code: `it("does something", { timeout: TEST_TIMEOUT_MS, retry: 2 }, () => {});`,
    },
    {
      name: "it.todo は持ち時間を取らない",
      code: `it.todo("will be written later");`,
    },
    {
      name: "describe は対象外",
      code: `describe("group", () => {});`,
    },
    {
      name: "テスト以外の3引数の呼び出しは対象外",
      code: `render("a", () => {}, 5000);`,
    },
    {
      name: "スプレッド渡しは静的に判定できないため対象外",
      code: `it(...args);`,
    },
    {
      name: "オプションにスプレッドが混ざる場合も対象外",
      code: `it("does something", { ...baseOptions }, () => {});`,
    },
  ],
  invalid: [
    {
      name: "it の持ち時間が省略されている",
      code: `it("does something", () => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "test の持ち時間が省略されている",
      code: `test("does something", () => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "it.each の持ち時間が省略されている",
      code: `it.each([1, 2])("handles %s", (n) => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "it.for の持ち時間が省略されている",
      code: `it.for([1, 2])("handles %s", (n) => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "タグ付きテンプレートの it.each で省略されている",
      code: "it.each`a`(\"handles $a\", ({ a }) => {});",
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "it.skip の持ち時間が省略されている",
      code: `it.skip("does something", () => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "オプションに timeout が入っていない",
      code: `it("does something", { retry: 2 }, () => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "持ち時間に数値を直接書いている",
      code: `it("does something", () => {}, 5000);`,
      errors: [{ messageId: "useSharedTimeout" }],
    },
    {
      name: "オプションの timeout に数値を直接書いている",
      code: `it("does something", { timeout: 5000 }, () => {});`,
      errors: [{ messageId: "useSharedTimeout" }],
    },
    {
      name: "持ち時間に別の識別子を渡している",
      code: `it("does something", () => {}, SOME_OTHER_TIMEOUT);`,
      errors: [{ messageId: "useSharedTimeout" }],
    },
    {
      name: "第3引数へオブジェクトを渡している",
      code: `it("does something", () => {}, { timeout: 5000 });`,
      errors: [{ messageId: "useSharedTimeout" }],
    },
  ],
});
