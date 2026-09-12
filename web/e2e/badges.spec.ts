import { test, expect, Page } from "@playwright/test";

/**
 * E3/E4 徽标的浏览器级回归：把"后端发了、前端没人看"的字段真正画出来。
 *
 * 前置：后端（mock）+ 前端 dev server 均在跑。
 *
 * 两个必须踩准的点（第一版就是在这两处写错的）：
 * 1. **发送按钮要用精确名**「发送」。宽泛的 /发送|分析/ 会先命中欢迎页的示例问句
 *    （"分析最近半年的月度销售趋势…"）——测试看起来在跑，实际发的是另一句话。
 * 2. **第二轮能不能增量，取决于第一轮的数据集有没有该列**。E3/03 的守卫是数据驱动的：
 *    上一结果没有 region 列时，下钻请求会被判为不可增量并**正确地回退全链**。
 *    所以第一轮必须先问一个能带出 region 的问题。
 */

async function newConversation(page: Page) {
  const btn = page.getByRole("button", { name: "新对话" });
  if (await btn.count()) await btn.first().click();
}

async function sendQuery(page: Page, query: string) {
  await page.getByPlaceholder(/输入|分析|问|query/i).first().fill(query);
  await page.getByRole("button", { name: "发送", exact: true }).first().click();
}

/** 等本轮彻底结束：导出按钮只在 FINISH 的报告卡里出现。 */
async function waitFinished(page: Page, nth: number) {
  await expect(page.getByRole("link", { name: /导出/ }).nth(nth)).toBeVisible({
    timeout: 90_000,
  });
}

test.describe("增量徽标", () => {
  // 两轮分析串行（每轮 ~10s）+ 导航，30s 的默认上限会中途掐断页面。
  test.setTimeout(180_000);

  test("第二轮下钻显示「增量 · 下钻」并标明未重新取数", async ({ page }) => {
    await page.goto("/");
    await newConversation(page);

    // 第一轮必须问出带 region 列的结果集，第二轮的下钻才有基线可用。
    await sendQuery(page, "分析各区域营收表现");
    await waitFinished(page, 0);

    await sendQuery(page, "基于上一结果，下钻到区域看营收");
    await waitFinished(page, 1);

    const badge = page.getByTestId("run-badges").last();
    await expect(badge).toBeVisible();
    // 文案必须能让人看出"这次只做了下钻"，而不是一次全量分析
    await expect(badge.getByText(/增量 · 下钻/)).toBeVisible();
    await expect(badge.getByText(/基于上一结果，未重新取数/)).toBeVisible();
  });

  test("首轮全链不显示增量徽标", async ({ page }) => {
    await page.goto("/");
    await newConversation(page);

    await sendQuery(page, "分析各区域营收表现");
    await waitFinished(page, 0);

    // 全链轮不该出现"增量"字样（否则徽标恒显示，等于噪音）
    await expect(page.getByText(/增量 · /)).toHaveCount(0);
  });
});
