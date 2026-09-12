import { test, expect, Page } from "@playwright/test";

/**
 * #6 E2E：导出流程端到端回归（浏览器级，补 test_ui.py 只验静态壳的缺口）。
 *
 * 前置：前端已起（npm run dev / 后端 web/dist）+ 后端可用。
 * 推荐用确定性环境实跑：
 *   MOCK_LLM=true 后端 + 样本库（scripts/generate_sample.py）
 *
 * 覆盖：能加载对话壳 → 能发起分析并到终端态 → 出现导出按钮 →
 *       导出端点真的返回可下载 zip → 分享/评论/权限协作骨架可见。
 */
async function sendQuery(page: Page, query: string) {
  const box = page.getByPlaceholder(/输入|分析|问|query/i).first();
  await box.fill(query);
  await page.getByRole("button", { name: /发送|分析|submit|Send/i }).first().click();
}

test.describe("数据分析控制台", () => {
  test("页面加载并渲染对话壳", async ({ page }) => {
    await page.goto("/");
    // 标题在头部与欢迎区各出现一次（strict mode 需取首个）
    await expect(page.getByText(/企业数据分析智能体/).first()).toBeVisible();
    // 输入框必须可用（未登录/未鉴权时也能进入）
    await expect(page.getByPlaceholder(/输入|分析|问|query/i).first()).toBeVisible();
  });

  test("发起分析后出现导出按钮，导出端点真返回 zip", async ({ page, request }) => {
    await page.goto("/");
    await sendQuery(page, "分析各区域营收表现并给出建议");

    // 等待分析进入终端态（FINISH）并渲染报告 → 出现导出链接
    const exportBtn = page.getByRole("link", { name: /导出/ }).first();
    await expect(exportBtn).toBeVisible({ timeout: 60_000 });

    // 导出端点 URL 形如 /api/v1/chat/analyze/export/<sid>?format=zip
    const href = await exportBtn.getAttribute("href");
    expect(href).toContain("/api/v1/chat/analyze/export/");
    expect(href).toContain("format=zip");

    // 真请求导出端点：应 200 且返回非空 zip（PK 魔数）
    const res = await request.get(href!);
    expect(res.status()).toBe(200);
    const body = await res.body();
    expect(body.length).toBeGreaterThan(0);
    expect(body[0]).toBe(0x50); // 'P'
    expect(body[1]).toBe(0x4b); // 'K'
  });

  test("分享/评论/权限协作骨架可见", async ({ page }) => {
    await page.goto("/");
    await sendQuery(page, "分析各渠道订单量");
    // 报告渲染后，协作骨架按钮出现
    await expect(page.getByRole("button", { name: /分享/ }).first()).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByRole("button", { name: /评论/ }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /权限/ }).first()).toBeVisible();
  });
});
