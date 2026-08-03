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
    globals: true,
    // 既定の 5000ms だと、テスト内の `waitFor` に 5 秒を指定した時点でテスト
    // 全体の持ち時間と並んでしまい、待ち切る前にテスト側が先にタイムアウト
    // する（Issue #35）。個々の待機より十分長く取り、待機が本当に失敗した
    // ときはタイムアウトではなく assert の失敗として原因が読める形にする。
    testTimeout: 20_000,
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/app/layout.tsx", "src/**/*.d.ts"],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 80,
        statements: 80,
      },
    },
  },
});
