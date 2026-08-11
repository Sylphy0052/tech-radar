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
      // `it.for` は位置引数の形を取らないため、オプション形式だけが正しい。
      name: "it.for はオプション形式で渡している",
      code: `it.for([1, 2])("handles %s", { timeout: TEST_TIMEOUT_MS }, (n) => {});`,
    },
    {
      name: "it.each もオプション形式で渡せる",
      code: `it.each([1, 2])("handles %s", { timeout: TEST_TIMEOUT_MS }, (n) => {});`,
    },
    {
      name: "it 本体もオプション形式で渡せる",
      code: `it("does something", { timeout: TEST_TIMEOUT_MS, retry: 2 }, () => {});`,
    },
    {
      name: "オプションにスプレッドが混ざっていても timeout が明示されていればよい",
      code: `it("does something", { ...baseOptions, timeout: TEST_TIMEOUT_MS }, () => {});`,
    },
    {
      name: "静的に解決できるキーで書いたオプションも読む",
      code: `it("does something", { ["timeout"]: TEST_TIMEOUT_MS }, () => {});`,
    },
    {
      name: "条件付きチェーンにも渡せる",
      code: `
        it.skipIf(isSlowMachine)("does something", () => {}, TEST_TIMEOUT_MS);
        it.runIf(hasGpu)("does something else", () => {}, TEST_TIMEOUT_MS);
      `,
    },
    {
      name: "テストを作る側の呼び出しは対象外",
      code: `
        const cases = it.each([1, 2]);
        const myTest = test.extend({ fixture: async (context, use) => use(1) });
      `,
    },
    {
      name: "it.todo は持ち時間を取らない",
      code: `it.todo("will be written later");`,
    },
    {
      name: "テスト本体を渡さない呼び出しも持ち時間を取らない",
      code: `
        it("pending test with no body");
        it.skip("stub, not yet implemented");
      `,
    },
    {
      name: "テスト名を変数で渡す呼び出しは見分けられないため対象外",
      code: `it(caseName, () => {});`,
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
      name: "timeout が無いオプションにスプレッドが混ざる場合は判定できないため対象外",
      code: `it("does something", { ...baseOptions }, () => {});`,
    },
    {
      name: "テスト本体が関数リテラルでない場合は位置を決められないため対象外",
      code: `
        it("does something", sharedBody, TEST_TIMEOUT_MS);
        it("does something else", condition ? optionsA : optionsB, () => {});
      `,
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
      name: "スプレッドに紛れていても明示された timeout は検査する",
      code: `it("does something", { timeout: 5000, ...baseOptions }, () => {});`,
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
    {
      name: "条件付きチェーンで持ち時間が省略されている",
      code: `it.skipIf(isSlowMachine)("does something", () => {});`,
      errors: [{ messageId: "missingTimeout" }],
    },
    {
      name: "it.for の持ち時間が省略されている",
      code: `it.for([1, 2])("handles %s", (n) => {});`,
      errors: [{ messageId: "useOptionsFormat" }],
    },
    {
      // 渡しても vitest に無視され、既定の5000msへ静かに戻る。
      name: "it.for へ持ち時間を位置引数で渡している",
      code: `it.for([1, 2])("handles %s", (n) => {}, TEST_TIMEOUT_MS);`,
      errors: [{ messageId: "useOptionsFormat" }],
    },
  ],
});
