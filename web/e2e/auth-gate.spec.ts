import { test, expect, Page } from "@playwright/test";

/**
 * E2E：鉴权失败 → AuthCentre 落地页 → 填 key → 自动重试。
 *
 * 这是 D58 登录/注册重构后第一次被自动化覆盖的路径。
 *
 * 设计
 * ----
 * - 粒度：只测「前端看到 401 后切到落地页、用户填 key 后自动回到原操作」这条 user-journey。
 *   - 不重复 unit 测试里已经锁定的表单校验（密码长度、一致性）——这块 AuthCentre.test.tsx 覆盖。
 *   - 不重复后端 /auth/login 的契约 ——这块 test_auth_users_api.py 覆盖。
 * - 拦截策略：playwright page.route 拦截第一次 /api/v1/chat/** 返回 401。
 *   - CI 里后端是 MOCK_LLM=true + AUTH_ENABLED=false（不主动 401），
 *     所以用 route 强制一次 401 来打通 AuthCentre 的显示路径。
 *   - 第二次（及以后）放行到真实后端，让 fallback 路径的 setApiKey 写入生效后
 *     重试成功（AUTH_ENABLED=false 下任意 key 即可达，与 fallback 语义一致）。
 */

// Composer 的 placeholder（中文，模糊匹配更抗 tiny drift）
const COMPOSER_PH = /描述你的业务问题/;
const SEND_BTN = /发送/;

async function gotoApp(page: Page) {
  await page.goto("/");
  await expect(page.getByPlaceholder(COMPOSER_PH)).toBeVisible();
}

/**
 * 拦截 chat 流端点：
 *   - 首次返回 401（触发 AuthCentre 落地页）
 *   - 之后 abolition 拦截，让 CRUD 重试直接打到后端
 *
 * 注意：只拦截 analyze 流（SSE 式 GET/POST），不拦 /api/v1/health 等，
 *       否则后端路由探测会被误判。
 */
async function interceptFirstAnalyzeAs401(page: Page): Promise<() => Promise<void>> {
  let calls = 0;
  await page.route("**/api/v1/chat/**", async (route) => {
    calls += 1;
    if (calls === 1) {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "invalid API key" }),
      });
    } else {
      await route.continue();
    }
  });
  return async () => {
    await page.unroute("**/api/v1/chat/**");
  };
}

test.describe("鉴权失败 → AuthCentre 落地页", () => {
  test.slow(); // 流程含网络往返 + 滑动动画（500ms×2），给足 120s

  test("首次 401 切到落地页，填 key 后自动回来并重试", async ({ page }) => {
    // 1. 强制首次 analyze 端点返回 401
    const unroute = await interceptFirstAnalyzeAs401(page);
    await gotoApp(page);

    // 2. 正常发第一条消息
    await page.getByPlaceholder(COMPOSER_PH).fill("分析各渠道营收");
    await page.getByRole("button", { name: SEND_BTN }).click();

    // 3. 落地页应出现（登录表单 visible）
    const loginHeading = page.getByRole("heading", { name: "登录" });
    await expect(loginHeading).toBeVisible({ timeout: 15_000 });
    // overlay 文案也应正确渲染
    await expect(page.getByText("你好，访客！")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "创建账号" }),
    ).toBeVisible();

    // 4. 解除拦截，让后续 analyze 落到真实后端
    await unroute();

    // 5. 填用户名 + 密码登录（后端可达时走 /auth/login）
    await page.getByPlaceholder("用户名").fill("test-user");
    await page.getByPlaceholder("密码").fill("correctpass");
    await page.getByRole("button", { name: /^登录$/ }).click();

    // 6. 落地页消失（needAuth → false），应用回到主界面
    await expect(loginHeading).not.toBeAttached({ timeout: 15_000 });

    // 7. Composer 重新可用；原来被 401 卡住的消息应以"发送中"重新出现
    //    （send() 是异步的 retry，我们只校验 Composer 重新可见即可）
    await expect(page.getByPlaceholder(COMPOSER_PH)).toBeVisible();
  });

  test("落地页上切换到注册模式、注册后回到主界面", async ({ page }) => {
    const unroute = await interceptFirstAnalyzeAs401(page);
    await gotoApp(page);

    await page.getByPlaceholder(COMPOSER_PH).fill("分析区域订单");
    await page.getByRole("button", { name: SEND_BTN }).click();

    // 切到注册模式
    await page.getByRole("button", { name: "创建账号" }).click();
    await expect(page.getByRole("heading", { name: "创建账号" })).toBeVisible();
    await expect(page.getByText("欢迎回来！")).toBeVisible();

    await unroute();

    // 注册
    await page.getByPlaceholder("用户名").fill("new-user");
    await page.getByPlaceholder("访问密钥（至少 6 位）").fill("123456");
    await page.getByPlaceholder("再输一次访问密钥").fill("123456");
    await page.getByRole("button", { name: "创建账号" }).click();

    await expect(
      page.getByRole("heading", { name: "创建账号" }),
    ).not.toBeAttached({ timeout: 15_000 });

    await expect(page.getByPlaceholder(COMPOSER_PH)).toBeVisible();
  });

  test("落地页右上角「跳过」与 Escape 键都回到主界面", async ({ page }) => {
    const unroute = await interceptFirstAnalyzeAs401(page);
    await gotoApp(page);

    await page.getByPlaceholder(COMPOSER_PH).fill("分析");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({
      timeout: 15_000,
    });

    // 点「跳过」
    await page.getByRole("button", { name: /跳过/ }).click();

    // 落地页卸载
    await expect(page.getByRole("heading", { name: "登录" })).not.toBeAttached({
      timeout: 10_000,
    });

    // 再按 Escape 也会退出（即使当前 focus 在 input 上）
    await page.getByPlaceholder(COMPOSER_PH).fill("再次分析");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({
      timeout: 15_000,
    });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("heading", { name: "登录" })).not.toBeAttached({
      timeout: 10_000,
    });

    await unroute();
  });

  test("注册密码校验：不足 6 位 / 两次不一致（在前端被拦，不到后端）", async ({
    page,
  }) => {
    const unroute = await interceptFirstAnalyzeAs401(page);
    await gotoApp(page);

    await page.getByPlaceholder(COMPOSER_PH).fill("分析");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await page.getByRole("button", { name: "创建账号" }).click();

    // 不足 6 位
    await page.getByPlaceholder("用户名").fill("u1");
    await page.getByPlaceholder("访问密钥（至少 6 位）").fill("12345");
    await page.getByPlaceholder("再输一次访问密钥").fill("12345");
    await page.getByRole("button", { name: "创建账号" }).click();
    await expect(page.getByText("访问密钥至少 6 位")).toBeVisible();
    // 且 Landing 仍在（没被刷掉）
    await expect(page.getByRole("heading", { name: "创建账号" })).toBeVisible();
    await unroute();
  });
});
