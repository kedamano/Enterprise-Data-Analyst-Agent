# C5/01 报告图内嵌 + 指标卡 + 导出预览 — 规格 v1.0（D51 定稿）

> 缺口（`docs/对标企业级Gap.md` §八 / 开发计划 §8.4-5）
> > | **UI 能力对齐** | 增量徽标、质量横幅、澄清卡片已做 | 待补**指标卡**与**导出预览** |
>
> 另外两处**已有的后端能力在 UI 上是断的**：
> 1. `visualization` 工具产出 PNG（`data/artifacts/<sid>/*.png`），但**报告里没有任何引用**，
>    也没有能取图的端点——图生成了，用户永远看不到（只在 `artifacts` 接口里见过文件名）。
> 2. 导出交付包（E5/03）只能**盲点**：用户不知道包里有什么（几份 CSV？有没有图？脱敏了没？），
>    点完才发现不是自己要的。

---

## 1. 报告图内嵌

### 1.1 图源：只认**真的画出来、且还在会话目录里**的图

`charts.collect_charts(state)` 从 `tool_results` 里挑：

| 条件 | 理由 |
|---|---|
| `tool == "visualization"` 且 `status == "SUCCESS"` | 失败的步骤没有产物 |
| `output.image_path` 非空 | 官方产物字段 |
| 路径 resolve 后**位于本会话工作目录之下** | 与 E5/03 导出同一条路径白名单纪律（防穿越） |
| 文件**真实存在** | 引用一张不存在的图 = 前端裂图 |

不满足任一条 → **静默不入列表**（不是错误，是"没图"）。返回
`[{name, title, chart_type, step_id, path}]`，`name` 为**文件名**（不含目录）。

### 1.2 内嵌：报告末尾追加 `## 图表`

```markdown
## 图表

![<title>](/api/v1/chat/analyze/chart/<session_id>/<name>)
```

- **无图则不出现该段落**（绝不留一个空标题）。
- 图**放在正文之后**：正文是主结论，图是佐证，不打断阅读。
- 引用用**绝对路径 URL**——报告可能被复制到别处（导出包/剪贴板），相对路径会失效。
- 报告同时进导出包（`report.md`），包里带 `charts/<name>`，故导出包里**图能显示**；
  贴到外部平台（不看 `/api/v1` 的）会裂图，这是**已知边界**，README 写明。

### 1.3 取图端点

`GET /api/v1/chat/analyze/chart/{session_id}/{name}`

| 情形 | 响应 |
|---|---|
| 正常 | `200` + `image/png`（`FileResponse`） |
| `name` 含路径分隔符 / `..` / 非白名单字符 | `400` |
| 文件不在会话工作目录之下 | `403` |
| 文件不存在 | `404` |
| AUTH 开启且非会话属主 | `403`（复用 `_assert_session_access`） |

**`name` 白名单**：`^[A-Za-z0-9_一-鿿.\-]+$`（viz 的 title 常为中文标题）。
先做字符白名单，再做 `resolve() + relative_to(workdir)` 双重校验——**两层都要**：
字符白名单挡 `%2e%2e%2f` 之外的直接穿越，`relative_to` 挡白名单漏网。

### 1.4 顺带修的**写侧**缺陷：viz 文件名未消毒

`viz_tool` 里 `wd / f"{title}.png"`，`title` 来自 LLM 参数（或 `ctx.objective[:30]`）。
title 含 `../` 时**图被写到会话目录之外**——读侧白名单再严也堵不住写侧。
故在 `viz_tool` 落盘前消毒文件名（剥目录分隔符与控制字符、去首尾点、空则 `chart`）。
这是**真实缺陷**（不是防御性编程）：LLM 给的 title 现在就能是任意串。

---

## 2. 指标卡（`metrics`）

### 2.1 归一在**后端**，前端只渲染

`AnalysisResult.metrics` 是 `list[dict]`，真实形态有两种（见 `report_tool` 的注释）：
结构化 `{name, value, comparison}` 与 coerce 后的纯文本 `{text}`。
SSE 的 FINISH 帧**此前根本不下发 metrics**（前端无从消费）。

契约：后端按**唯一口径**归一后下发，元素恒为：

```json
{"name": "华东营收", "value": "1.23 亿", "comparison": "环比 +8.1%"}
```

- `{text}` 形态 → `name=text`、`value=""`、`comparison=""`（内容不丢）；
- `name` 键取 `name | text | metric`（与 `report_tool` **同一优先级**，两处不许分叉）；
- 全空串的元素**丢弃**（不渲染空气泡）；
- 无 metrics → `[]`（前端**整块不渲染**，不是渲染一个空壳）。

### 2.2 前端

`MetricCards` 组件渲染 `data-testid="metric-cards"`，每张卡显示 name / value / comparison。
取**最后一个非空** `metrics`（与 `latestIteration` 同范式；FINISH 帧才有）。
空数组 / null → 返回 `null`（不占位）。

---

## 3. 导出预览

### 3.1 **预览与实际包同源**（本卡最重要的一条纪律）

`GET /api/v1/chat/analyze/export/{session_id}/manifest?masked=`

返回：

```json
{"session_id": "...", "masked": true, "total_bytes": 12345,
 "files": [{"name": "report.md", "bytes": 3210, "kind": "report",
            "note": "报告正文（含图表引用）"}, ...]}
```

**`files` 必须由与 zip 完全相同的那一个构造函数产出**——预览说有几个文件、各多少字节，
zip 里就必须是同样那几个、同样那些字节。两处各写一份清单 = 预览迟早骗人。

- `masked` 缺省判定与 zip **同一函数**（策略未激活时缺省=0，保持既有行为）；
- 预览**不需要两步授权**（HITL）：它只暴露文件名与字节数，不暴露任何**值**；
- 会话不存在 → 404；AUTH 越权 → 403（与既有端点一致）。

### 3.2 脱敏版**不含图表**

图是位图，**无法逐像素脱敏**。故 `masked=1` 时 `charts/*.png` 一律**不进包**，
并在 README 与 manifest 的 `note` 里写明原因——宁可少给，不可假装脱敏过。

### 3.3 前端 `ExportPreview`

报告卡片上「导出」旁新增「导出预览」按钮：

- 点击 → 拉 manifest → 展示文件名 / 大小 / 备注；
- 底部给「下载交付包」链接（指向真实 zip 端点，`masked` 与预览一致）；
- 失败（401/403/404）→ 显示错误行，**不静默**；
- 请求必须经 `api.ts`（带 `authHeaders()`），不得裸 `fetch`。

---

## 4. 验收

| 项 | 判据 |
|---|---|
| 报告图内嵌 | 有图 → 报告含 `## 图表` 与绝对 URL；无图 → 无该段落 |
| 取图端点 | 正常 200 / 穿越 400 / 不存在 404 / 越权 403 |
| 指标卡 | SSE FINISH 帧带归一化 `metrics`；两种输入形态都归一正确；空则 `[]` |
| 导出预览 | manifest 的 `files` 与 zip 的 `namelist()` **逐项相等**（同一条测试） |
| 脱敏 | `masked=1` 时 zip 与 manifest 都无 `charts/` |
| 前端 | `npm run build` 通过（tsc -b + vite build）；源码级契约钉住三件事：① `Markdown` 的 `img` 渲染器**真的输出 `<img>` 且未被降级成链接**（验收卡的"且前端渲染"必须有对应断言）② 指标卡只渲染不判断（归一在后端）③ 导出预览走 `fetchManifest` 而非裸 `fetch(` |
