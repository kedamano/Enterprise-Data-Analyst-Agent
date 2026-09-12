# E5/03 分析交付物导出 — 规格 v1.0

> 动因（计划 D25）：分析师做完分析要**交出去**——贴周报、发同事、存档复盘。
> 现在报告只在界面上，SQL 散在 trace 接口，CSV 散在 artifacts 接口，**没有"一次拿走"的产物**。
> 计划原文：实现 stats 说明字段 + 导出端点 `GET /export?session=`（SQL/CSV/报告）；UI 按钮。

## 1. 端点

`GET /api/v1/chat/analyze/export/{session_id}?format=zip|sql|report|csv`

| format | 返回 | Content-Type |
|---|---|---|
| `zip`（默认） | 一个交付包（见 §2） | `application/zip` |
| `sql` | 本次运行的全部只读 SQL（含步骤目标注释） | `text/plain; charset=utf-8` |
| `report` | 报告正文（Markdown，含溯源/口径/质量段落） | `text/markdown; charset=utf-8` |
| `csv` | 仅数据产物的 zip（一个或多个 CSV） | `application/zip` |

- 会话不存在 → **404**（与 `/analyze/artifacts`、`/analyze/trace` 一致，便于前端统一处理）。
- 运行失败（无报告）也能导出：给出手上有的 SQL/数据，并在 README 里写明失败原因（**不假装成功**）。

## 2. 交付包（`format=zip`）内容

```
report.md          报告正文（含 ## 数字来源 / ## 口径说明 / ## 数据质量与限制）
queries.sql        本次运行的全部 SQL，按执行顺序，带 step_id 与步骤目标注释
data/<step>.csv    数据产物（**原始值，不脱敏** —— 分析师本机产物需可用，见 E4/02）
trace.json         溯源清单（claim → sql_id → SQL → 行样本）
README.txt         交付说明：目标/时间/状态/口径与质量限制/脱敏说明/文件清单
```

- **脱敏边界**：上下文侧脱敏（E4/02）不影响导出——`queries.sql` 里是查询语句本身，
  `data/*.csv` 是原始数据；README 里显式写明这一点，避免误以为导出物也脱敏。

## 3. 安全
- **路径白名单**：只打包位于该会话工作目录（`data/artifacts/<session_id>/`）**之下**的文件。
  越界路径（`../` 或绝对路径指向别处）**跳过并记入 README**，绝不打包（防路径穿越）。
- **会话隔离**：只读该 `session_id` 自己的 checkpoint 与工作目录。
- 不新增写操作：导出是纯读。

## 4. 边界
- 无 SQL、无 CSV（如纯问答）→ 仍返回包，README 说明"本次无查询/数据产物"。
- 同名 CSV 冲突 → 以 `step_id.csv` 命名即天然唯一（工具已按此落盘）。
- 大包：不做流式；CSV 受 `sql_max_rows` 约束，zip 体积可控。

## 5. TDD
| 用例 | 断言 |
|---|---|
| zip 内容完整 | 含 `report.md`/`queries.sql`/`README.txt`/`trace.json`，且至少一个 `data/*.csv` |
| zip 里的 CSV 是原始值 | 含被脱敏列时，CSV 内仍是原值（与 E4/02 的上下文脱敏分离） |
| SQL 不含 DML | 导出的 SQL 全部通过 `guard_readonly_sql` |
| `format=sql` | `text/plain`，含 SQL 文本与 step_id |
| `format=report` | `text/markdown`，与 checkpoint 报告一致 |
| 404 | 不存在的会话 |
| 路径穿越 | 伪造越界 artifact → **不**进包，README 记一条跳过说明 |
| 无产物的会话 | 仍 200，README 说明无数据产物 |
