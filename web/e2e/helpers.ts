import { Page, expect } from "@playwright/test";

/** Composer 输入框 placeholder（模糊匹配，抗 tiny drift） */
export const COMPOSER_PH = /描述你的业务问题/;
/** 发送按钮 */
export const SEND_BTN = /发送/;

/**
 * 拦截指定端点：首次返回 401（触发 AuthCentre 落地页），后续放行。
 * 返回解除拦截的函数。
 */
export async function interceptFirst401(
  page: Page,
  urlGlob: string,
): Promise<() => Promise<void>> {
  let calls = 0;
  await page.route(urlGlob, async (route) => {
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
    await page.unroute(urlGlob);
  };
}

/**
 * 拦截所有 /api/v1 后端请求，返回 mock 数据。用于 chat.spec / command-palette.spec
 * 等不需要触发 401 的流程。
 */
export async function mockAllApi(page: Page): Promise<void> {
  // 后端健康探测
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

  // 登录 / 注册 → 返回 mock token
  await page.route("**/api/v1/auth/login", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        token: "mock-token",
        user: { id: "u1", username: "alice", role: "analyst", display_name: "Alice" },
      }),
    }),
  );

  await page.route("**/api/v1/auth/register", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        token: "mock-token",
        user: { id: "u1", username: "alice", role: "analyst", display_name: "Alice" },
      }),
    }),
  );

  // chat analyze stream → 返回 mock SSE
  await page.route("**/api/v1/chat/analyze/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        'data: {"status":"INIT","message":"初始化"}',
        "",
        'data: {"status":"FINISH","message":"完成","report":"# Mock Report\\n\\nHello from mock!"}',
        "",
        "data: [DONE]",
        "",
      ].join("\n"),
    }),
  );

  // 注意：jobs CRUD 由各测试文件自行 mock（jobs.spec 需要带状态），此处不拦截。
}

/**
 * 访问首页并等待 composer 可见。
 * 注意：应用是 JIT 鉴权——落地页不在初始加载时出现，只在收到 401 后才弹出。
 */
export async function gotoApp(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByPlaceholder(COMPOSER_PH)).toBeVisible({ timeout: 15_000 });
}

/**
 * 公共登录步骤：先触发 401 弹出落地页，再填用户名密码 + 提交 + 等待 composer 出现。
 *
 * 由于后端可能未启动，AuthCentre.login() 在 fetch 失败时会 fallback 到
 * setApiKey() 直接写 localStorage，所以登录总能成功。
 */
export async function login(
  page: Page,
  username = "alice",
  password = "password123",
): Promise<void> {
  // 1. 拦截首次 analyze → 401，触发落地页
  const unroute = await interceptFirst401(page, "**/api/v1/chat/**");

  // 2. 发一条消息 → 触发 401 → 落地页出现
  await gotoApp(page);
  await page.getByPlaceholder(COMPOSER_PH).fill("登录触发");
  await page.getByRole("button", { name: SEND_BTN }).click();

  // 3. 等待登录 heading 出现
  const loginHeading = page.getByRole("heading", { name: "登录" });
  await expect(loginHeading).toBeVisible({ timeout: 15_000 });

  // 4. 解除拦截（让重试能成功）
  await unroute();

  // 5. 填用户名 + 密码 + 提交
  // 注意：AuthCentre 同时渲染 login 和 register 两个 form（用 opacity 切换可见性），
  // 所以 getByPlaceholder("用户名") 会命中 2 个元素。这里限定在 accessible 的表单上。
  // login heading 所在容器的下一个 form 就是登录表单
  const loginForm = page.locator("form").filter({ has: page.getByRole("heading", { name: "登录" }) });
  await loginForm.getByPlaceholder("用户名").fill(username);
  await loginForm.getByPlaceholder("密码").fill(password);
  await loginForm.getByRole("button", { name: /^登录$/ }).click();

  // 6. 等待 composer 落地页消失 + composer 重新可见
  await expect(loginHeading).not.toBeAttached({ timeout: 15_000 });
  await expect(page.getByPlaceholder(COMPOSER_PH)).toBeVisible({ timeout: 15_000 });
}

/**
 * 仅 mock /health 以让 RuntimeBadge 显示正常，不拦截其他请求。
 * 用于 OIDC 测试等不需要 401 的场景。
 */
export async function mockHealthOnly(page: Page): Promise<void> {
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
}
