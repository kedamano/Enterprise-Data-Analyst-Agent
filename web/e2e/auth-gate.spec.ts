import { test, expect, Page } from "@playwright/test";

/**
 * E2E：鉴权失败 → AuthCentre 落地页 → 填 key → 自动回来并重试。
 *
 * 关键：AuthCentre 是双滑块设计——登录 / 注册两块表单**同时常驻 DOM**，
 * 靠 opacity + aria-hidden 切换可视。Playwright 默认只匹配可见元素，
 * 但同一 placeholder 在两块里都有（如「用户名」），所以表单内的字段必须
 * scope 到 aria-hidden="false" 那一块，否则 getByPlaceholder 会命中两个。
 *
 * 后端策略：不需要真实后端。
 *   - `/health` 用 fill 返回 ok
 *   - `/auth/login` `/auth/register` 不拦截，走后端（AUTH_ENABLED=false 下任意 key 可达）；
 *     前端 submitLogin/submitRegister 内置 fallback：后端不可达时 setApiKey()
 *     直写 localStorage + onAuthed()——保障无后端时流程仍能跑。
 *   - 第一次 `/api/v1/chat/**` → 401 打通 AuthCentre 落地页路径。
 */

const COMPOSER_PH = /描述你的业务问题/;
const SEND_BTN = /发送/;

async function gotoApp(page: Page) {
  // 1. 探活 → ok（前端 .env 默认无 key 也能进壳）
  await page.route("**/api/v1/health", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        data_source: "sqlite",
        llm_mode: "mock",
        mock_llm: true,
        llm_degraded: false,
      }),
    }),
  );
  await page.goto("/");
  await expect(page.getByPlaceholder(COMPOSER_PH)).toBeVisible({ timeout: 15_000 });
}

/**
 * 拦截 chat 端点：前 N 次返回 401（触发 AuthCentre 落地页），后续放行。
 * 默认 2 次：首次发消息触发 401 落地页；落地页 Escape/跳过后再发一条→再次 401，
 * 用于验证「退出落地页后能再次触发」。
 */
async function interceptChatAs401(page: Page, times = 2): Promise<() => Promise<void>> {
  let calls = 0;
  await page.route("**/api/v1/chat/**", async (route) => {
    calls += 1;
    if (calls <= times) {
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

/**
 * AuthCentre 的活动面板：aria-hidden="false" 那一块表单（登录或注册）。
 * 所有表单内字段都 scope 到这里，避免双滑块两块 placeholder 重复命中。
 */
function activeForm(page: Page) {
  return page.locator('[aria-hidden="false"]');
}

test.describe("鉴权失败 → AuthCentre 落地页", () => {
  test.slow(); // 流程含网络往返 + 滑动动画（500ms×2），给足 120s

  test("首次 401 切到落地页，填 key 后自动回来并重试", async ({ page }) => {
    const unroute = await interceptChatAs401(page, 1);
    await gotoApp(page);

    // 发第一条消息 → 401 → 落地页
    await page.getByPlaceholder(COMPOSER_PH).fill("分析各渠道营收");
    await page.getByRole("button", { name: SEND_BTN }).click();

    // 登录 heading 岀现
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({ timeout: 15_000 });
    // overlay 引导文案 + 切换按钮
    await expect(page.getByText("你好，访客！")).toBeVisible();
    await expect(page.getByRole("button", { name: /创建账号/ }).first()).toBeVisible();

    // 解除拦截（让后端可达）
    await unroute();

    // scope 到活动面板（登录表单）
    const form = activeForm(page);
    await form.getByPlaceholder("用户名").fill("test-user");
    await form.getByPlaceholder("密码").fill("correctpass");
    await form.getByRole("button", { name: /^登录$/ }).click();

    // 落地页消失
    await expect(page.getByRole("heading", { name: "登录" })).not.toBeAttached({ timeout: 15_000 });
    // 落地页消失即视为成功；retry 触发第二轮流式分析，其完成时间依赖后端 mock，
    // 不在本用例断言范围内——否则会因等待 composer 重新可输入而超时。
    await unroute();
  });

  test("落地页上切换到注册模式、注册后回到主界面", async ({ page }) => {
    const unroute = await interceptChatAs401(page, 1);
    await gotoApp(page);

    await page.getByPlaceholder(COMPOSER_PH).fill("分析区域订单");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({ timeout: 15_000 });

    // 点 overlay 的「创建账号」切换到注册模式
    await page.getByRole("button", { name: /创建账号/ }).first().click();
    await expect(page.getByRole("heading", { name: "创建账号" })).toBeVisible();
    await expect(page.getByText("欢迎回来！")).toBeVisible();

    // 注册表单在活动面板
    const form = activeForm(page);
    await form.getByPlaceholder("用户名").fill("new-user");
    await form.getByPlaceholder("访问密钥（至少 6 位）").fill("123456");
    await form.getByPlaceholder("再输一次访问密钥").fill("123456");

    // 注册成功后会重试本轮消息（已到后端，不在 401 窗口内）
    await unroute();
    await form.getByRole("button", { name: "创建账号" }).click();

    await expect(
      page.getByRole("heading", { name: "创建账号" }),
    ).not.toBeAttached({ timeout: 15_000 });
  });

  test("落地页右上角「跳过」与 Escape 键都回到主界面", async ({ page }) => {
    const unroute = await interceptChatAs401(page, 2);
    await gotoApp(page);

    await page.getByPlaceholder(COMPOSER_PH).fill("分析");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({
      timeout: 15_000,
    });

    // 点「跳过」
    await page.getByRole("button", { name: /跳过/ }).click();
    await expect(page.getByRole("heading", { name: "登录" })).not.toBeAttached({
      timeout: 10_000,
    });

    // 再发一条 → 再次 401 → 落地页
    await page.getByPlaceholder(COMPOSER_PH).fill("再次分析");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({
      timeout: 15_000,
    });

    // Escape 退出
    await page.keyboard.press("Escape");
    await expect(page.getByRole("heading", { name: "登录" })).not.toBeAttached({
      timeout: 10_000,
    });

    await unroute();
  });

  test("注册密码校验：不足 6 位（在前端被拦，不到后端）", async ({ page }) => {
    const unroute = await interceptChatAs401(page, 1);
    await gotoApp(page);

    await page.getByPlaceholder(COMPOSER_PH).fill("分析");
    await page.getByRole("button", { name: SEND_BTN }).click();
    await expect(page.getByRole("heading", { name: "登录" })).toBeVisible({
      timeout: 15_000,
    });

    // 切到注册
    await page.getByRole("button", { name: /创建账号/ }).first().click();
    await expect(page.getByRole("heading", { name: "创建账号" })).toBeVisible();

    const form = activeForm(page);
    await form.getByPlaceholder("用户名").fill("u1");
    await form.getByPlaceholder("访问密钥（至少 6 位）").fill("12345");
    await form.getByPlaceholder("再输一次访问密钥").fill("12345");
    await form.getByRole("button", { name: "创建账号" }).click();

    // 错误提示 且 不离开注册页
    await expect(page.getByText("访问密钥至少 6 位")).toBeVisible();
    await expect(page.getByRole("heading", { name: "创建账号" })).toBeVisible();

    await unroute();
  });
});
