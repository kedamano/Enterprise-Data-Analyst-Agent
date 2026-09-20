import { test, expect } from "@playwright/test";
import { mockHealthOnly, gotoApp, interceptFirst401, COMPOSER_PH, SEND_BTN } from "./helpers";

/**
 * 登录态相关 E2E：
 * - 未登录访问首页显示登录框（通过触发 401 让 JIT 落地页出现）
 * - OIDC 未配置时"使用 OIDC 登录"按钮隐藏
 * - OIDC 配置后按钮可见
 *
 * 注意：应用是 JIT 鉴权——落地页不在初始加载时出现，只在收到 401 后才弹出。
 */

test.describe("AuthCentre 登录态", () => {
  test.beforeEach(async ({ page }) => {
    await mockHealthOnly(page);
  });

  test("未登录访问首页显示登录框", async ({ page }) => {
    // 拦截首次 analyze → 401，触发落地页
    const unroute = await interceptFirst401(page, "**/api/v1/chat/**");
    await gotoApp(page);

    // 发一条消息触发 401
    await page.getByPlaceholder(COMPOSER_PH).fill("登录触发");
    await page.getByRole("button", { name: SEND_BTN }).click();

    // 落地页应出现
    const loginHeading = page.getByRole("heading", { name: "登录" });
    await expect(loginHeading).toBeVisible({ timeout: 15_000 });
    // overlay 文案
    await expect(page.getByText("你好，访客！")).toBeVisible();
    // 用户名输入框可见（只校验 login 表单中的那个，register 表单也有同 placeholder 但 opacity=0）
    const visibleUsername = page.getByPlaceholder("用户名").first();
    await expect(visibleUsername).toBeVisible();

    await unroute();
  });

  test("OIDC 未配置时按钮隐藏", async ({ page }) => {
    // 默认不 mock OIDC config 端点（让它返回 404 → configured=false）
    const unroute = await interceptFirst401(page, "**/api/v1/chat/**");
    await gotoApp(page);
    await page.getByPlaceholder(COMPOSER_PH).fill("登录触发");
    await page.getByRole("button", { name: SEND_BTN }).click();

    const loginHeading = page.getByRole("heading", { name: "登录" });
    await expect(loginHeading).toBeVisible({ timeout: 15_000 });

    // OIDC 按钮不存在
    const oidcBtn = page.getByText("使用 OIDC 登录");
    await expect(oidcBtn).toHaveCount(0);

    await unroute();
  });

  test("OIDC 配置后显示按钮", async ({ page }) => {
    // 模拟 OIDC 已配置
    await page.route("**/api/v1/auth/oidc/config", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ configured: true, issuer: "https://auth.example.com" }),
      }),
    );

    // getOidcConfig 是在 AuthCentre 挂载时调用的，所以需要先让落地页出现
    // 但由于 fetchOidcConfig 用 try/catch 兜底，即使网络失败也会默认 false
    // 这里直接拦截 analyze → 401 → 落地页出现 → 同时 oidc/config 返回 configured:true
    const unroute = await interceptFirst401(page, "**/api/v1/chat/**");
    await gotoApp(page);
    await page.getByPlaceholder(COMPOSER_PH).fill("登录触发");
    await page.getByRole("button", { name: SEND_BTN }).click();

    const loginHeading = page.getByRole("heading", { name: "登录" });
    await expect(loginHeading).toBeVisible({ timeout: 15_000 });

    const oidcBtn = page.getByText("使用 OIDC 登录");
    await expect(oidcBtn).toBeVisible({ timeout: 10_000 });

    await unroute();
  });
});
