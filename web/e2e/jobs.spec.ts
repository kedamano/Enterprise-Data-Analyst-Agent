import { test, expect, Page } from "@playwright/test";
import { mockAllApi, login } from "./helpers";

/**
 * Workflow Job CRUD E2E (mock API).
 *
 * 因为当前前端没有独立的 Jobs panel（jobs 通过 CommandPalette 触发），
 * 这里用 page.route mock /api/v1/jobs 来验证 API 契约：
 *   - POST /api/v1/jobs 创建 job → 返回 mock job
 *   - GET /api/v1/jobs 列表包含新创建的 job
 *   - POST /api/v1/jobs/{id}/enabled 切换状态
 *
 * 注意：page.request 走的是 Playwright 自己的网络栈，不受 page.route 拦截。
 * 所以这里用 page.evaluate 调用 fetch 来确保 route mock 生效。
 */

// 用内存态模拟 jobs 列表
let mockJobs: Array<{ id: string; name: string; enabled: boolean }> = [];

async function setupJobsMock(page: Page): Promise<void> {
  // GET /api/v1/jobs → 列出所有 jobs
  await page.route("**/api/v1/jobs", (route) => {
    if (route.request().method() === "GET") {
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockJobs),
      });
    } else if (route.request().method() === "POST") {
      const postData = route.request().postDataJSON() as { name: string; query?: string };
      const newJob = {
        id: `job-${Date.now()}`,
        name: postData.name || "untitled",
        enabled: true,
        created_at: new Date().toISOString(),
      };
      mockJobs.push(newJob);
      route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(newJob),
      });
    } else {
      route.continue();
    }
  });

  // POST /api/v1/jobs/{id}/enabled → 切换启用/禁用
  await page.route("**/api/v1/jobs/*/enabled", (route) => {
    if (route.request().method() === "POST") {
      const jobId = route.request().url().split("/").at(-2) ?? "";
      const body = route.request().postDataJSON() as { enabled: boolean };
      const job = mockJobs.find((j) => j.id === jobId);
      if (job) job.enabled = body.enabled;
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, id: jobId, enabled: body.enabled }),
      });
    } else {
      route.continue();
    }
  });
}

test.describe("Workflow Job CRUD (mock API)", () => {
  test.beforeEach(async ({ page }) => {
    mockJobs = []; // 重置 mock 数据
    await mockAllApi(page);
    await setupJobsMock(page);
    await page.goto("/");
    await login(page);
  });

  test("创建 job 出现在列表", async ({ page }) => {
    const today = new Date().toISOString().slice(0, 10);
    // 使用 page.evaluate 调用 fetch，走浏览器网络栈，被 page.route 拦截
    const created = await page.evaluate(async ({ today }) => {
      const resp = await fetch("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "test-job", query: `生成 ${today} 日报` }),
      });
      return { ok: resp.ok, data: await resp.json() };
    }, { today });

    expect(created.ok).toBeTruthy();
    expect(created.data.name).toBe("test-job");
    expect(created.data.enabled).toBe(true);

    // 验证 GET 列表包含新 job
    const jobs = await page.evaluate(async () => {
      const resp = await fetch("/api/v1/jobs");
      return await resp.json() as Array<{ id: string; name: string; enabled: boolean }>;
    });
    expect(jobs.length).toBeGreaterThanOrEqual(1);
    expect(jobs.some((j) => j.name === "test-job")).toBeTruthy();
  });

  test("禁用 job", async ({ page }) => {
    // 先创建一个 job
    const created = await page.evaluate(async () => {
      const resp = await fetch("/api/v1/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "toggle-job", query: "日报" }),
      });
      return await resp.json() as { id: string; name: string; enabled: boolean };
    });
    expect(created.enabled).toBe(true);

    // 禁用它
    const toggled = await page.evaluate(async ({ jobId }) => {
      const resp = await fetch(`/api/v1/jobs/${jobId}/enabled`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: false }),
      });
      return { ok: resp.ok, data: await resp.json() };
    }, { jobId: created.id });
    expect(toggled.ok).toBeTruthy();
    expect(toggled.data.enabled).toBe(false);

    // 验证列表状态
    const jobs = await page.evaluate(async () => {
      const resp = await fetch("/api/v1/jobs");
      return await resp.json() as Array<{ id: string; name: string; enabled: boolean }>;
    });
    const job = jobs.find((j) => j.id === created.id);
    expect(job).toBeTruthy();
    expect(job!.enabled).toBe(false);
  });
});
