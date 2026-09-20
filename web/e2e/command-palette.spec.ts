import { test, expect } from "@playwright/test";
import { mockAllApi, login } from "./helpers";

/**
 * cmdk 命令面板 E2E：
 * - Ctrl-K 唤醒面板
 * - 点击命令触发视图切换
 */

test.describe("Command Palette", () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApi(page);
    await login(page);
  });

  test("Ctrl-K 唤醒面板", async ({ page }) => {
    // 按 Ctrl+K 打开命令面板
    await page.keyboard.press("Control+k");
    // cmdk 输入框可见
    const cmdkInput = page.getByPlaceholder("搜索命令…");
    await expect(cmdkInput).toBeVisible({ timeout: 5_000 });
  });

  test("点击命令触发视图切换", async ({ page }) => {
    // 打开命令面板
    await page.keyboard.press("Control+k");
    const cmdkInput = page.getByPlaceholder("搜索命令…");
    await expect(cmdkInput).toBeVisible({ timeout: 5_000 });

    // 点击 "切换到 统计" 命令 (对应 analytics view)
    const analyticsCmd = page.getByText("切换到 统计", { exact: true });
    await expect(analyticsCmd).toBeVisible();
    await analyticsCmd.click();

    // 验证 URL hash 变化
    await expect.poll(() => page.evaluate(() => window.location.hash)).toContain("analytics");
  });
});
