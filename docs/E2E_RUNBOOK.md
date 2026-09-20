# E2E 运行手册（Playwright）

前端 E2E 测试Playwright 编写，覆盖 4 个核心场景：落地页（401 退出 / 切换登录注册 / 跳过 / Escape）、冒烟（应用徽标 / 双滑块切换 / 新建对话）、双徽标联动（180s 等待分析结果 + 徽标推入）、导出（分析完成后的文件导出路径）。

---

## 1. CI 自动跑（推荐）

仓库的 GitHub Actions (`.github/workflows/ci.yml`) 已配置 E2E job：

```bash
# CI 流程简化示意（真实 job 在 .github/workflows/ci.yml 内）
- uses: actions/setup-node@v4
- run: npm ci
- run: npx playwright install --with-deps chromium   # CI 国际出口不受限
- run: pip install -r requirements.txt
- run: uvicorn da.main:app --host 0.0.0.0 --port 8000 &   # 起真实后端
- run: npm run build && npm run preview -- --port 5173 &   # 起前端
- run: npx playwright test
- uses: actions/upload-artifact@v4:
    path: playwright-report/
```

每次 PR 自动跑；失败会在 PR 里 Upload `playwright-report/index.html` 作为 artifact。

---

## 2. 本机跑（需绕过 proxy）

本机 npm proxy 指向死代理（127.0.0.1:7890），`npx playwright install chromium` 会静默卡住、无输出。两种方式绕开：

### 方式 A：用国内镜像（最稳，企业内网常见）

```bash
# PowerShell / bash 通用
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"
npx playwright install chromium
```

安装成功后：

```bash
# 一次起两个服务 + 跑测试
Start-Process pwsh -ArgumentList "-Command", "uvicorn da.main:app --host 0.0.0.0 --port 8000" -NoNewWindow
Start-Process pwsh -ArgumentList "-Command", "npm run build && npm run preview -- --port 5173 --strictPort" -NoNewWindow

# 等后端起来再跑
Start-Sleep -Seconds 5
npx playwright test
```

### 方式 B：直接禁掉 proxy

```bash
# Git Bash / bash
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
npx playwright install chromium
```

### 方式 C：had-been 装过、复用已有缓存

Playwright 浏览器存在 `~/.cache/ms-playwright/`（Linux/macOS）或 `%USERPROFILE%\AppData\Local\ms-playwright\`（Windows）。若你曾在别的机器装过，把这个目录拷过来即可免下载。

---

## 3. 运行参数速查

| 目标 | 命令 |
|---|---|
| 全量 E2E | `npx playwright test` |
| 单文件 | `npx playwright test e2e/auth-gate.spec.ts` |
| 只想看 headless 跑 | 默认已是 |
| headed 看浏览器 | `npx playwright test --headed` |
| 调试模式（Inspector） | `npx playwright test --debug --grep "落地"` |
| 只跑 chromium（默认） | 无需额外参数 |
| 强制重跑 flake | `npx playwright test --retries=0 --workers=1` |

---

## 4. 输出产物

| 产物 | 位置 | 何时生成 |
|---|---|---|
| 控制台 report（pass/fail 计数） | stdout | 始终 |
| `playwright-report/index.html` | 本目录 | 始终 |
| `test-results/` 截图 | 本目录 | 失败时自动截图（`playwright.config.ts` 已配 `screenshot: "only-on-failure"`） |
| `test-results/` 录像 | 本目录 | 失败时自动录像（`video: "retain-on-failure"`） |

---

## 5. 常见 Flake 与绕开手法

| 现象 | 原因 | 绕开 |
|---|---|---|
| `expect(locator).toBeVisible()` 30s 超时 | 后端分析耗时 > 默认 expect timeout | 单条 `toBeVisible({ timeout: 90_000 })`；徽标 case 已设 `setTimeout(180_000)` |
| "偶发超时，有时过有时不过" | 测试级 timeout 是 120s，UI expect 级是 10s；分析较慢时 expect 先于业务完成触发 | 关键断言一律加显式 `timeout` |
| "locator 匹配两个元素" | `getByText("登录")` 同时命中 heading + 按钮 | 用 `within(activePane())` 或 `getByRole("button", { name: /登录/ })` 精确匹配 |
| "后端没起来先跑前端" |  | 确保 backend `GET /health` 返回后再跑 E2E；本地脚本参考上方"方式 A" |

---

## 6. 覆盖现状（4 文件）

| 文件 | 覆盖场景 | case 数 |
|---|---|---|
| `e2e/smoke.spec.ts` | 徽标切换 / 双滑块 / 新建对话 | 3 |
| `e2e/auth-gate.spec.ts` | 401 落地页 / 退出 / Escape | 4 |
| `e2e/badges.spec.ts` | 分析结果徽标推入 · 双徽标联动 | 2 |
| `e2e/export.spec.ts` | 分析完成后导出路径 | 2 |

---

> **铁律（Iron Rule 6）**：测评结果以真实跑通的截图/报告为准，不凭文档数据声称"通过"。当前本机被 proxy 墙挡住，CI 是测评真实通过率的可信通道。
