import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

import requireTestTimeout from "./eslint-rules/require-test-timeout.mjs";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // vitest のカバレッジ出力は生成物のため対象外にする。
    "coverage/**",
  ]),
  {
    // テストの持ち時間の付け忘れを機械的に止める（Issue #47）。拡張子を絞らず
    // テストファイル全体を対象にして、置き場所が増えたときの検査漏れを防ぐ。
    files: ["**/*.{test,spec}.{ts,tsx,mts,mjs,js,jsx}"],
    plugins: {
      techradar: { rules: { "require-test-timeout": requireTestTimeout } },
    },
    rules: {
      "techradar/require-test-timeout": "error",
    },
  },
]);

export default eslintConfig;
