import { defineConfig, devices } from "@playwright/test";

/**
 * #6 E2E 配置：对运行中的前端（默认 http://localhost:5173，由 `npm run dev` 或
 * 后端托管 `web/dist` 提供）跑浏览器级回归。
 *
 * 用法：
 *   cd web && npm install && npx playwright install
 *   npm run dev            # 另一个终端起前端
 *   npx playwright test    # 或 npm run test:e2e
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
