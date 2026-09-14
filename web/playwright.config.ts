import { defineConfig, devices } from "@playwright/test";

/**
 * #6 E2E 配置：对运行中的前端（默认 http://localhost:5173，由 `npm run dev` 或
 * 后端托管 `web/dist` 提供）跑浏览器级回归。
 *
 * 用法：
 *   cd web && npm install && npx playwright install
 *   npm run dev            # 一个终端起前端
 *                          #   ⚠️ 后端也要起：页面要连 /api/v1，只起前端会全红
 *   npx playwright test    # 或 npm run test:e2e
 *
 * 两处曾经写错、值得留痕的地方
 * ---------------------------
 * 1. **`timeout` 要按"跑完一轮真实分析"来定，不是按普通 UI 测试的 30s。**
 *    原值 `30_000` —— 而 spec 里写的是 `toBeVisible({ timeout: 60_000 })`，
 *    意图是"等分析结束"。但 **测试级 30s 上限先生效**，那句 60s 永远等不到，
 *    用例在 30s 被掐断，表现为"偶发超时"。
 *    两条徽标用例各自 `setTimeout(180_000)` 绕开了，而 `export.spec.ts`
 *    **没有绕** —— 它是潜伏的必现 flake（分析稍慢就红）。
 *    这里统一提到 120s：单轮分析（mock）约 10–40s，两轮用例仍保留自己的 180s。
 *
 * 2. **reporter 要真的产出报告。** 只配 `["list"]` 时不会生成
 *    `playwright-report/`，CI 的 "Upload Playwright report" 步骤会**上传空目录**
 *    （CI 里用 `--reporter=list,html` 绕开过，见 .github/workflows/ci.yml）。
 *    配置才是正解：CI 下同时出 html，本地只出 list（不落一堆文件）。
 */
export default defineConfig({
  testDir: "./e2e",
  // 见上方说明 1：按"一轮真实分析跑完"定，而非 UI 测试的秒级预期
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  // 见上方说明 2：CI 下产出 playwright-report/index.html 供上传
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : [["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:5173",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
