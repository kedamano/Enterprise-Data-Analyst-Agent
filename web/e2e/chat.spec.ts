import { test, expect } from "@playwright/test";
import { mockAllApi, login, COMPOSER_PH, SEND_BTN } from "./helpers";

/**
 * 核心分析流 E2E：
 * - 登录成功进入 chat 视图
 * - 发送消息显示 loading 帧 → 出现回复
 *
 * 通过 helpers.login() 完成 JIT 鉴权落地页登录流程。
 */

test.describe("Chat 核心分析流", () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApi(page);
    await login(page, "alice", "password123");
  });

  test("登录成功进入 chat 视图", async ({ page }) => {
    // composer 可见即视为 chat 视图
    const composer = page.getByPlaceholder(COMPOSER_PH);
    await expect(composer).toBeVisible();
    // 登录 heading 应已消失
    await expect(page.getByRole("heading", { name: "登录" })).toHaveCount(0);
  });

  test("发送消息显示 loading 帧 → 出现回复", async ({ page }) => {
    const composer = page.getByPlaceholder(COMPOSER_PH);
    const sendBtn = page.getByRole("button", { name: SEND_BTN });

    // 输入文字并发送
    await composer.fill("hello");
    await sendBtn.click();

    // 等待 assistant 消息出现 (mock SSE 返回 FINISH 帧 → report 渲染)
    await expect(page.locator("text=Mock Report").first()).toBeVisible({ timeout: 15_000 });

    // composer 清空后可以再输入
    await expect(composer).toHaveValue("");
  });
});
