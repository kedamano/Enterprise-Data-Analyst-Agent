import path from "node:path";
import { test, expect } from "@playwright/test";

/**
 * D / 2026-09-17：最低限度的 UI smoke。
 *
 * 不断言 LLM 真回、不依赖后端——只验证前端 dev server 能把页面骨架
 * （落地页 + Composer 输入框）渲染到 DOM，并截一张全页 png 快照。
 *
 * 定位策略：
 *  - 没用 welcome heading（中文落地页 heading 文案在不同 feature 分支会变）
 *  - 用 Composer placeholder 定位（稳定、跨分支不变）
 *  - 用提交按钮文本"发送"（长期稳定）
 *
 * 截图：
 *   playwright `test.info().outputPath()` 返回 test-results/<test>/绝对路径，
 *   不依赖工作目录，也跟随 CI e2e job 的 `web/test-results/` 上传步骤。
 *   文件命名 `ui_smoke_YYYY-MM-DD-<timestamp>.png`。
 */

test.describe("UI skeleton smoke", () => {
  test("首页落地页渲染 + Composer 可见 + 截图", async ({ page }, testInfo) => {
    // 确保存在（CI 里的 test-results 默认有，本地不一定）
    const stamp = new Date().toISOString().slice(0, 10);
    const fileName = `ui_smoke_${stamp}-${Date.now()}.png`;
    const shotPath = testInfo.outputPath(fileName);
    // outputPath 里父目录已存在；如需手写子目录就 mkdir
    const shotDir = path.dirname(shotPath);
    await page.goto("/", { waitUntil: "networkidle" });

    // ① react 真渲染出来了：root 非空
    const root = page.locator("#root");
    await expect(root).not.toHaveText("", { timeout: 15_000 });
    await expect(root.locator("visible=true").first()).toBeVisible();

    // ② Composer 输入框——固定 placeholder，跨 feature 分支不变
    const composer = page.getByPlaceholder(/描述你的业务问题/, {
      exact: false,
    });
    await expect(composer.first()).toBeVisible({ timeout: 10_000 });

    // ③ 提交按钮——"发送"
    const send = page.getByRole("button", { name: /发送/ }).first();
    await expect(send).toBeVisible();

    // ④ 全页截图（落到 test-results/）
    await page.screenshot({ path: shotPath, fullPage: true });

    // ⑤ 填查询并提交，验证 UI 反应（mock 1s 出结果——在前端表现为清空输入框）
    await composer.first().fill("smoke test query");
    await send.click();

    // 提交后：composer 被清空（loading 中作者消息气泡出现）
    await expect(composer.first()).toHaveValue("", { timeout: 15_000 });

    // 备注：截图绝对路径打到 CI 日志里，方便排查
    console.log("[smoke] screenshot saved at", shotPath);
  });
});
