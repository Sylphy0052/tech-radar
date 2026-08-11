import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    // 同じworktreeで複数プロセス同時に `npm test` を実行しても、孤児ディレクトリの
    // 掃除（`coverage/` 配下）が互いに干渉しないよう、セッション開始時に一度だけ実行する。
    globalSetup: ["./vitest.global-setup.ts"],
    globals: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/app/layout.tsx", "src/**/*.d.ts"],
      // 既定値（`coverage`）を複数プロセスが共有すると、片方が
      // 「Something removed the coverage directory ... Vitest created earlier」で
      // 落ちる（Issue #33）。プロセスごとのPIDでサブディレクトリを分離する。
      // `.gitlab-ci.yml` の artifacts は `frontend/coverage/` を丸ごと収集するため
      // ネストしても影響しない。異常終了で残った孤児ディレクトリは
      // `vitest.global-setup.ts` が次回実行時に掃除する。
      reportsDirectory: `coverage/${process.pid}`,
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 80,
        statements: 80,
      },
    },
  },
});
