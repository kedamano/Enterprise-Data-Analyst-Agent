import { test, expect, Page } from "@playwright/test";

/**
 * #6 E2E：导出流程端到端回归（浏览器级，补 test_ui.py 只验静态壳的缺口）。
 *
 * 设计
 * ----
 * 不依赖真实后端：全部端点经 page.route mock。
 *   - /health → ok
 *   - /chat/analyze/stream → FINISH + report → ChatMessage 里渲染 Report + 导出链接
 *   - /chat/analyze/export/<sid> → 真实 zip 字节（PK 魔数 + report.md 在包内）
 *
 * 为什么不用真实后端：
 *   1. 依赖真实 LLM / mock_llm 后端会在 CI 启动阶段带来不确定性（队列、延迟）、
 *      与本次要测的"前端渲染 → 导出按钮 → 下载链路"无关。
 *   2. 原来用 bare APIRequestContext（独立于浏览器 page）+
 *      request.get(href) 相对路径发到 nowhere——mock 也不共享。
 *      改走 page 级 download 事件，mock 由 page.route 覆盖。
 */

/** 最小可用 SSE：FINISH 帧带报告 → 前端 ChatMessage 渲染"导出"链接 */
function mockChatAnalyze(page: Page) {
  const sseBody = [
    'data: {"status":"INIT","message":"初始化"}',
    "",
    'data: {"status":"PLAN","message":"制定计划"}',
    "",
    'data: {"status":"EXECUTE","message":"执行中"}',
    "",
    'data: {"status":"FINISH","message":"完成","report":"# 分析报告\\n\\n区域 A 营收 120 万，环比 +12%。"}',
    "",
    "data: [DONE]",
    "",
  ].join("\n");

  return page.route("**/api/v1/chat/analyze/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sseBody,
    }),
  );
}

/** mock /health 让 RuntimeBadge 不报探活失败 */
function mockHealth(page: Page) {
  return page.route("**/api/v1/health", (route) =>
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

/** CRC32（标准多项式，直接计算——文件内容小、只调一次） */
function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    crc ^= bytes[i];
    for (let j = 0; j < 8; j++) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/**
 * 构造**真实 zip** 字节：PK 魔数 + report.md 中文内容。
 * 用 page.route 拦截 GET /analyze/export/<sid> 返回——浏览器走下载流程，
 * 与生产链路等价（Content-Type: application/zip + attachment）。
 */
async function mockExportZip(page: Page) {
  const encoder = new TextEncoder();
  const fileName = "report.md";
  const fileContent = "# 分析报告\n\n区域 A 营收 120 万，环比 +12%。";
  const nameBytes = encoder.encode(fileName);
  const contentBytes = encoder.encode(fileContent);

  const crc = crc32(contentBytes);
  const lfh = new Uint8Array(30 + nameBytes.length + contentBytes.length);
  const dv = new DataView(lfh.buffer);
  dv.setUint32(0, 0x04034b50, true);
  dv.setUint16(4, 20, true);
  dv.setUint16(6, 0, true);
  dv.setUint16(8, 0, true);
  dv.setUint16(10, 0, true);
  dv.setUint16(12, 0, true);
  dv.setUint32(14, crc, true);
  dv.setUint32(18, contentBytes.length, true);
  dv.setUint32(22, contentBytes.length, true);
  dv.setUint16(26, nameBytes.length, true);
  dv.setUint16(28, 0, true);
  lfh.set(nameBytes, 30);
  lfh.set(contentBytes, 30 + nameBytes.length);

  const cdh = new Uint8Array(46 + nameBytes.length);
  const dv2 = new DataView(cdh.buffer);
  dv2.setUint32(0, 0x02014b50, true);
  dv2.setUint16(4, 20, true);
  dv2.setUint16(6, 20, true);
  dv2.setUint16(8, 0, true);
  dv2.setUint16(10, 0, true);
  dv2.setUint16(12, 0, true);
  dv2.setUint16(14, 0, true);
  dv2.setUint16(16, crc, true);
  dv2.setUint32(20, contentBytes.length, true);
  dv2.setUint32(24, contentBytes.length, true);
  dv2.setUint16(28, nameBytes.length, true);
  dv2.setUint16(30, 0, true);
  dv2.setUint16(32, 0, true);
  dv2.setUint16(34, 0, true);
  dv2.setUint16(36, 0, true);
  dv2.setUint32(38, 0, true);
  dv2.setUint32(42, 0, true);
  cdh.set(nameBytes, 46);

  const eocd = new Uint8Array(22);
  const dv3 = new DataView(eocd.buffer);
  dv3.setUint32(0, 0x06054b50, true);
  dv3.setUint16(4, 0, true);
  dv3.setUint16(6, 0, true);
  dv3.setUint16(8, 1, true);
  dv3.setUint16(10, 1, true);
  dv3.setUint32(12, cdh.length, true);
  dv3.setUint32(16, lfh.length, true);
  dv3.setUint16(20, 0, true);

  const zipBytes = new Uint8Array(lfh.length + cdh.length + eocd.length);
  zipBytes.set(lfh, 0);
  zipBytes.set(cdh, lfh.length);
  zipBytes.set(eocd, lfh.length + cdh.length);

  await page.route("**/api/v1/chat/analyze/export/**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/zip",
      headers: { "Content-Disposition": 'attachment; filename="analysis.zip"' },
      body: Buffer.from(zipBytes.buffer),
    }),
  );
}

async function sendQuery(page: Page, query: string) {
  const box = page.getByPlaceholder(/描述你的业务问题/);
  await box.first().fill(query);
  await page.getByRole("button", { name: /发送/ }).first().click();
}

async function gotoApp(page: Page) {
  await mockHealth(page);
  await mockChatAnalyze(page);
  await mockExportZip(page);
  await page.goto("/");
  await expect(page.getByPlaceholder(/描述你的业务问题/)).toBeVisible({ timeout: 15_000 });
}

test.describe("数据分析控制台", () => {
  test.beforeEach(async ({ page }) => {
    await mockHealth(page);
    await mockChatAnalyze(page);
    await mockExportZip(page);
  });

  test("页面加载并渲染对话壳", async ({ page }) => {
    await gotoApp(page);
    // 标题在头与欢迎区各出现一次，严格模式下取 .first()
    await expect(page.getByText(/企业数据分析智能体/).first()).toBeVisible();
  });

  test("发起分析后出现导出按钮，导出端点真返回 zip", async ({ page }) => {
    await gotoApp(page);
    await sendQuery(page, "分析各区域营收表现并给出建议");

    // 等分析完成 → 出现导出链接
    const exportBtn = page.getByRole("link", { name: /导出/ }).first();
    await expect(exportBtn).toBeVisible({ timeout: 60_000 });

    // href 形如 /api/v1/chat/analyze/export/<sid>?format=zip
    const href = await exportBtn.getAttribute("href");
    expect(href).toContain("/api/v1/chat/analyze/export/");
    expect(href).toContain("format=zip");

    // 走 page 级 download 事件（bare APIRequestContext 不继承 page.route mock）
    const [download] = await Promise.all([
      page.waitForEvent("download", { timeout: 15_000 }),
      exportBtn.click(),
    ]);

    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream) chunks.push(chunk as Buffer);
    const buf = Buffer.concat(chunks);
    expect(buf.length).toBeGreaterThan(0);
    expect(buf[0]).toBe(0x50); // 'P'
    expect(buf[1]).toBe(0x4b); // 'K'
  });

  test("分享/评论/权限协作骨架可见", async ({ page }) => {
    await gotoApp(page);
    await sendQuery(page, "分析各渠道订单量");

    // 报告渲染后协作骨架按钮出现
    await expect(page.getByRole("button", { name: /分享/ }).first()).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByRole("button", { name: /评论/ }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /权限/ }).first()).toBeVisible();
  });
});
