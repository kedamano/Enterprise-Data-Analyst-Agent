# E2 自由写码（Free-form Code）— 规格 v1.0（D8 定稿）

> 目标：Analyst 阶段允许模型产出自定义分析步骤，突破"模板只读取数"，仍受守卫约束。
> 借 DeepAnalyze 的"写码-执行"范式；守卫沿用既有 sql/python 安全层。

## 1. 两种形态（Analyst 输出中新增可选步骤字段）
- `sql_text`：一段只读 SQL（可选、由 SQL lint/白名单守卫执行）
- `python_code`：一段分析脚本（可选、由 AST 守卫 + 沙箱执行）
- `confirm: bool = false`：高危/自定义步骤的人审标记（UI 后续消费）
- 每步自动产生 `step_id` 与溯源（复用 E1：产出数字 evidence 需链到该步）

## 2. 守卫（复用既有，不改松）
- SQL：`sql_tool` 只读正则 + 单语句 + 行数上限 + 语句级超时（**写入/多语句拒绝**）。
- Python：`python_tool` AST 模块黑名单 + 禁 eval/exec/compile + 子进程/Docker 沙箱 + 超时。
- 自由码执行失败 → 结构化错误回注 → 下一轮重写（已有 REPLAN 反馈通道）。

## 3. 执行流
Analyst(带自定义步骤) → executor 新分支：
- 命中 `sql_text` → `freeform.execute_free_sql(sql_text, session)` → ToolResult(step=自由步骤)
- 命中 `python_code` → `freeform.execute_free_python(...)` → ToolResult
- 未命中 → 沿用确定性参数合成（既有行为不变）

## 4. 可观测 / 边界
- 每步入 tool_results + 审计 + trace（与普通步骤一致）。
- **默认关闭 flag**：`config.freeform_enabled=False`，E2 期间测试环境开；上线默认关，需显式允许。
- mock 与 real 一致：mock 也能产出并执行自由 SQL（确定性样例）。

## 5. TDD（E2 期间）
- 自由 join/window SQL 执行并返回行（mock 断言非空、行数受限）。
- 注入/DML/多语句拒绝（复用负路径）。
- 自由 Python 危险模块/逃逸拒绝。
- 失败自纠错：自由码失败→错误回注→重写成功。
- eval 增"自定义分析"mock golden。
