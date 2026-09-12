# 数据分析 Agent 优化方案

> 依据：Anthropic《构建高效智能体》七种模式架构图 + 两个真实使用案例复盘
> 日期：2026-09-10

---

## 零、结论先行

编排骨架没有问题。你的 Agent 已经把 Anthropic 七种模式里的 **5 种做完了**（增强型 LLM、
提示链、路由、协调者-工作者、评估者-优化者），第 6 种（并行化）做了一半。

真正的短板**不在模式本身，而在模式之间的信息传递质量**。两个案例暴露的三个断点全部属于
同一类问题：**结构化的东西在流转过程中退化成了文本**。

| 断点 | 退化表现 | 直接后果 |
|---|---|---|
| 附件 → 工具 | 用户的 CSV 只有"列名+行数+5行样例"进入提示词，从未注册为可查询数据源 | 计划仍指向内置库，查了 100 行无关数据 |
| 工具结果 → 报告 | Reporter 只拿到 `AnalysisResult`，拿不到 `tool_results` 原始证据 | 输出只能是空泛模板 |
| 反思 → 环境 | Reflection 靠 LLM 自我判断，缺少确定性的事实校验 | REPLAN 三轮原地打转 |

---

## 一、案例复盘

### 案例一：sleep.csv（失败）

用户上传 400 行睡眠健康数据，问「这份文件反应了什么数据规律」。

实际发生的事：

1. 附件被渲染成文本拼进 query（列名 13 个 + 前 5 行样例）
2. `build_plan()` 返回 `requires_sql=True` —— **附件从未注册成工具可寻址的数据源**
3. Executor 去查 `sample_enterprise.db`，`schema_search` 报告「发现 1 张表」
4. `sql_query` 返回 100 行**与睡眠无关**的数据
5. Reflection 判定证据不足 → **REPLAN 3 轮，三轮计划几乎相同**
6. 预算耗尽 → best-effort 报告，置信度 0.30 / 0.50
7. Reporter 落到 mock 模板：「Mock分析师未做深度统计推断」

**验证证据**（实测复现）：

```
detect_mode('这份文件反应了什么数据规律')        → full
classify_task(...)                              → full_analysis
build_plan(...)['requires_sql']                 → True      ← 断点
'分析这份csv' in query                          → False     ← 附件文本未改变路由
```

### 案例二：同一条数据，人工分析（成功）

用户把同一份 sleep.csv 交给 Claude 直接分析，得到了**非常完整**的结论：压力与睡眠质量的
负相关、睡眠时长的 U 型关系、肥胖与呼吸暂停的 2.3 倍风险、职业差异表、性别差异……

这份分析说明了两件事：

**① 数据本身信息量充足。** 13 个字段的横截面数据足以支撑描述性统计 + 分组对比 + 相关性
观察。Agent 拿不到这些结论，纯粹是工程链路问题，不是数据问题。

**② 成功的原因是"数据全程在上下文里"。** 人工分析时，整份 CSV 都在模型上下文中，它可以
随时回看任意一行、做任意维度的交叉。而 Agent 的链路把数据"切碎"了：只留 5 行样例给
Planner，真实数据留在工具里但不指向附件。

> 值得注意：案例二里模型自己的思考过程有明显的不确定性（"我数一下"、"大概 30%"），
> 最终数字是估算的。这恰恰说明 **Agent 相比人工分析的正确性优势应该来自工具，而不是
> 来自模型** —— 但这要求工具能访问到正确的数据。

### 两个案例的对比结论

| 维度 | 案例一（Agent） | 案例二（人工） |
|---|---|---|
| 数据可及性 | 5 行样例（提示词） | 全部 400 行 |
| 统计计算 | 无 | 模型心算（不精确） |
| 分组交叉 | 失败 | 多个维度 |
| 结论质量 | Mock 模板 | 结构化洞察 |
| 可复现性 | 高（有 trace） | 无 |

**理想形态应该是两者的并集**：工具负责精确计算，模型负责解释与叙事。

---

## 二、优化清单

按「收益 / 成本」排序，P0 是必须做的。

### P0-1　让附件成为一等数据源（解决案例一的根本问题）

这是**唯一一个能让 sleep.csv 类场景从失败变成功的改动**。

当前状态：附件只是提示词里的文本。

目标状态：附件注册成工具可寻址的数据源。

实现要点：

1. **落地为物理文件**：`AttachmentStore.put()` 时除了解析摘要，再把原始字节写到
   `data/uploads/<session_id>/<filename>`，返回一个稳定路径。
2. **注册成虚拟表**：在 `schema_search` 中加入该 session 的附件清单，让 Planner 能"发现"它；
   若数据源是 SQLite，可直接 `ATTACH` 或建临时表 `upload_sleep`。
3. **修正 `build_plan()`**：当 `attachment_context` 非空时，计划应产出
   `dataset_profile(upload_sleep)` → `sql_query` / `python_analysis` 指向该文件，
   而不是内置库。
4. **修正路由**：`detect_mode()` 应识别"用户上传了文件"这一事实。当前 `_PY_HINTS` 里有
   "处理csv""分析这份csv"等关键词，但用户实际说的是"这份文件反应了什么数据规律"，
   **一个都没命中**。建议改为结构性判断：`if attachment_type == 'table': return PY 或 FULL`。

预期效果：sleep.csv 场景下 Agent 会真正 profile 这份数据、算出相关系数、按 occupation
分组统计，而不是查 100 行别的表。

### P0-2　Reporter 补上原始证据

当前状态：`run_reporter()` 只把 `state.analysis.model_dump()` 传给 LLM。

问题：报告里不可能出现模型没见过的数字。所以只能产出"未显式计算指标"这类空话。

修复：把 `tool_results` 的实际数据（尤其是聚合查询的结果行）一并传给 Reporter：

```python
raw = _llm("reporter", json.dumps({
    "analysis": state.analysis.model_dump(),
    "tool_results": [r.model_dump() for r in state.tool_results],   # 新增
    "attachment": state.attachment_context,                          # 新增
}, ensure_ascii=False, default=str), json_mode=False)
```

同时：mock 兜底文案需要改。**"Mock分析师未做深度统计推断"这句话不该出现在面向用户的报告里** ——
应该改成诚实的失败说明（"未能获取到足够证据，原因是……"），而不是暴露内部实现。

### P0-3　Reporter 失败要显式降级，而不是静默 mock

当前 `use_mock_llm or not tpl.get("report")` 这个判断会让 LLM 调用失败时静默回退到模板，
用户看不出区别。应该：

- 保持回退行为（保证可用性）
- 但在报告里**明确标注**这是降级产物，并在 `metadata` 里记录原因
- 前端用醒目的提示条展示，而不是混在正常报告里

### P1-1　引入并行化（Anthropic 第 4 种模式）

当前 Executor 严格串行，且**同一个计划可以连续 REPLAN 三轮完全一样**（案例一）。

两个改进方向：

**A. 无依赖的步骤并行执行。** 计划步骤已有 `dependencies` 字段，
`run_executor` 可以改成按拓扑层批量执行无依赖步骤。对"同时看多个维度"的场景能显著降
低延迟。

**B. 多假设并行投票（sectioning）。** 对同一问题让 3 个"分析员"分别从不同角度分析，
再由聚合器合并。这是 Anthropic 明确推荐的模式，特别适合数据分析这种"结论可能有多种
合理解释"的场景。

### P1-2　打破 REPLAN 空转

案例一里三轮 REPLAN 计划几乎相同 —— 这是纯粹的浪费。

现有 `_replan_key()` 已经能检测"同计划+同目标"，`_REPLAN_MAX_STALL = 2`。
但看起来触发门槛太高（三轮都跑完了才 FAILED）。

建议：
- 把停滞检测提前到 **第 2 轮**就中断
- REPLAN 时**强制变更**：要求 planner 必须换表/换字段/换工具，且新旧计划不能完全一致
  （现有 prompt 里已经写了这条要求，但没有校验）

### P1-3　长期记忆回写（Anthropic 第 7 种模式的关键缺口）

有 `memory/long_term.py`，但从案例看，Agent 没有从"失败"中学习。

建议：把每次 FAILED / 低置信度的运行，连同失败原因写回长期记忆。下次遇到相似问题时，
Planner 能看到"上次这样的计划失败了，因为……"。

### P2-1　附件内容直接进上下文（小文件场景）

对于案例二这种小文件（27KB），其实可以**直接把全部内容放进上下文**，让它像人工分析
一样流畅。策略：

- 附件 < 阈值（比如 50KB / 2000 行）→ 全文进上下文
- 超过阈值 → 走 P0-1 的工具寻址路径

这样小文件场景可以同时拿到"人工分析的数据可及性"和"工具的精确计算"。

### P2-2　图片附件接入视觉

当前图片只登记元信息。若 LLM 支持多模态（GPT-4o / Claude / DeepSeek-VL），
图表截图类附件价值很高。可先做"图片 → 标题/描述"的预处理，再把描述并入上下文。

---

## 二·补　P0-4　降级链路可见化（本次真实模型复现暴露）

**问题**：当 LLM 调用失败并静默回落到确定性实现时，编排层只记录
`degraded = true`，前端和报告里只有一句笼统的提示。本次复现中 **5 个阶段全部 403 降级**，
但用户只能看到"当前处于降级模式"，既不知道**哪一级降了**，也不知道**为什么降**。

这会让排障成本极高 —— 正如案例一里，用户看到的是"Mock分析师未做深度统计推断"，
而真正的原因（403、熔断 OPEN）被藏在了日志深处。

**证据**（本次运行的 `llm_fallbacks`，5 条，覆盖全部 LLM 阶段）：

| 阶段 | 错误 |
|---|---|
| `context` | 403 This model is not available in your region. |
| `planner` | 同上 |
| `analyst` | 同上 |
| `reflection` | 同上 |
| `reporter` | 同上 |

**建议**：

1. **报告顶部**加一个「运行健康度」区块，列出降级阶段与原因，例如：

   ```
   ⚠️ 本次运行有 5 个阶段降级：context / planner / analyst / reflection / reporter
      原因：403 This model is not available in your region.
      影响：结论仅基于工具原始数据，未做统计推断与因果分析。
   ```

2. **SSE 事件**里把 `llm_fallbacks` 从"挂在末尾"提到独立事件，前端可以实时亮黄条。
3. **`/health`** 里把 `llm_fallbacks_total` 按阶段分组暴露，便于监控告警。

**为什么优先级是 P0**：它不增加功能，但直接决定了"出问题时人能不能看懂"。
本次复现已经证明降级管道健壮（403 不崩、仍产出真实数值），
那么把这份健壮**如实讲出来**，就是最低成本、最高收益的一步。

---

## 三、优先级与工作量

| 优先级 | 项目 | 影响 | 复杂度 | 状态 |
|---|---|---|---|---|
| P0-1 | 附件注册为数据源 | 极高 | 中 | ✅ 已完成 |
| P0-2 | Reporter 补原始证据 | 高 | 低 | ✅ 已完成 |
| P0-3 | 报告降级显式化 | 中 | 低 | ✅ 已由 P0-4 闭环（报告/编排/前端三处均显式） |
| **P0-4** | **降级链路可见化** | **高** | **低** | ✅ 已完成（分类/聚合/渲染 + 报告顶栏 + SSE 帧 + /health） |
| **P1-1** | **并行化编排** | **中高** | **中高** | ✅ 已完成（波次并发执行器 + 顺序退化 + 失败占位） |
| P1-2 | 打破 REPLAN 空转 | 中 | 低 | ✅ 已由根因 C 修复实质解决 |
| **P1-3** | **长期记忆回写** | **中** | **中** | ✅ 已完成（降级/失败教训写回 + 回读通道） |
| **P2-1** | **小文件全文进上下文** | **中** | **低** | ✅ 已完成（<50KB 且 <2000 行灌入 prompt） |
| **P2-2** | **图片视觉接入** | **低** | **中** | ✅ 已完成（多模态 image_analyze 工具 + 原图落地 + Planner 感知） |
| **P2-3** | **LLM 动态路由与限流韧性** | **高** | **低** | ✅ 已完成（provider 排序 + 模型回退链 + 429 尊重 Retry-After） |

**实施进度**：P0-x 全绿；P1-x（P1-1/P1-2/P1-3）全绿；P2-x 中 P2-1/P2-2/P2-3 已完成。**优化方案全部闭环。**
本轮新增的六项（P0-4 / P1-1 / P1-3 / P2-1 / P2-2 / P2-3）均已在确定性层实现并通过单测 + 集成测试验证。

> ⚠️ **前置条件已变化（2026-09-11）**：原 OpenRouter 凭证由 402（余额不足）转为可调用，
> 但 `google/gemma-4-31b-it:free` 是**免费共享池**，真实端到端首轮即被上游 **429** 限流。
> 已针对性落地 **P2-3 动态路由**（见下）来消除这一类故障，而不再依赖"换凭证"这种外部条件。
>
> **P2-3 实测结论**：修复后同一条真实端到端**跑完整条链路并 FINISH**（见「三·补 P2-3 实测」），
> 其中 `context`/`planner`/`analyst`/`reflection`/`reporter` 各阶段均出现「主模型 429 → 自动切换
> 备用模型 → 成功」的迁移记录。这同时完成了一直被凭证阻塞的 **P1-1/P2-1/P2-2 真实模型复测**。

---

## 三·补　本轮新增四项实现纪要（2026-09-11）

四项均为**确定性层**改动，不依赖 LLM，已通过单测 + 集成测试验证（见各测试文件）。

### P0-4　降级链路可见化
- 新增 `app/infrastructure/llm/degradation.py`：`classify_error`（错误串→结构化根因，
  `region_blocked`/`auth_failed`/`quota_exhausted`/…，`unknown` 时保留原文不吞信息）、
  `summarize_degradation`（按阶段聚合 + 影响范围 + 人话摘要）、`render_degradation_block`（Markdown）。
- 三处暴露：报告顶部「运行健康度」区块（`nodes._prepend_degradation_block`）、
  SSE 帧 `degradation` 字段（`chat._degradation_view`）、`/health` 的
  `llm_degraded_stages` / `llm_degradation` / `llm_fallbacks_by_stage`。
- 测试：`tests/test_degradation_visibility.py`（22 项）。

### P1-1　并行化编排
- 原 `graph` 的「逐步骤 while 循环」替换为 `run_executor_all`：按依赖做**拓扑分波**，
  同波内依赖无关步骤用 `ThreadPoolExecutor` 并发；`parallel_executor_workers <= 1` 退化为顺序。
- 失败/恢复产生的 `llm_fallbacks` 由 router 自身记录，执行器不触碰；依赖失败的后继步骤
  打 FAILED 占位，不卡死、不漏步。
- 顺带补的 P0-1 缺口：`dataset_profile` 此前只查内置库、对上传表报 `no such table`；
  现识别上传表后直连边车库做画像（`profile_tool.run` + 注入 `_session_id`）。
- 测试：`tests/test_parallel_executor.py`（4 项，含确定性 sleep.csv 端到端）。

### P1-3　长期记忆回写
- `_persist_memory` 的 Reflexion 教训回流从「仅 state.error / FAIL」扩展到**降级**（mock 兜底
  但 FINISH 的静默失败），复用 P0-4 的 `classify_error` 给出**可操作规避动作**，写回
  `long_term`（类型 `lesson`，`category=llm_degradation`）。
- 新增回读通道 `long_term.recent_lessons()`，并在 `run_context` 把历史降级教训注入
  `known_degradation_risks`，闭环「写→读」。
- 测试：`tests/test_long_term_lesson_writeback.py`（5 项）。

### P2-1　小文件全文进上下文
- `DatasetPreview.is_small`（字节≤50KB 且 行数≤2000）命中时，`to_context()` 把整张表
  渲染成 CSV 代码块灌入 prompt，模型可就地计算、省去 SQL 往返；JSON 解析也补齐 `all_rows`。
- 测试：`tests/test_small_file_full_context.py`（7 项）。

### P2-2　图片视觉接入
- **附件层**：`DatasetPreview` 新增 `path` 字段；`to_context()` 增加 `image` 分支，
  明确提示「必须用 `image_analyze` 实际读取图片，不能只凭文件名臆测」；`build_preview()`
  对图片返回 `kind="image"`；新增 `image_dir()` / `attached_images()` 辅助。
- **原图落地**：`/attachments/upload` 对图片把原图写入 `data/uploads/<session>/images/<name>`
  并写入 `preview.path`（此前只登记元信息、声明"暂未接入"，是个半吊子状态）。
- **多模态网关**：`BaseLLM` 新增 `vision(system, user, image_paths, ...)`；`OpenAILLM.vision`
  把本地图编码成 base64 data URL 多模态 content 调用（含熔断 + 降级到 MockLLM）；
  `MockLLM.vision` 返回**带具体数值**的确定性描述（与 sleep.csv 案例同思路，避免「未获取到事实数据」）。
  `config.llm_vision_model` 空则回退 `llm_model`。
- **工具**：新增 `image_analyze`（`vision_tool.run`）—— 按 `_session_id` + 文件名定位落地图、
  调 `get_llm().vision()` 提取结构化描述；已注册进 `REGISTRY` 并加入 `_session_id` 注入集合
  （**根因 F**：多模态工具也必须透传 session_id 才能寻址上传图）；`specs.py` 增加对应 ToolSpec。
- **Planner 感知**：`run_planner` 把 `uploaded_images` 作为**结构化字段**喂给 Planner
  （与 `uploaded_datasets` 平行，同样避免模型忽略多模态输入）；`MockLLM._stage_planner` 在存在
  图片时首步发出 `image_analyze`。`planner.md` / `executor.md` 工具清单补充 `image_analyze`。
- 测试：`tests/test_image_vision.py`（9 项：附件类型/上下文/列表、上传落地+path、
  Mock 成功/缺图失败/缺参失败、run_executor_all 端到端、MockLLM 首步发 image_analyze）。

### P2-3　LLM 动态路由与限流韧性
**触发**：真实端到端（`google/gemma-4-31b-it:free`）首轮即在 `context` 阶段被
OpenRouter 免费共享池 **429**，重试 45s 后整跑失败。这是"免费模型能否用于真实验证"的卡点。

- **provider 动态路由**：`provider_routing_body()` / `_extra_body()` 往请求体注入
  `provider={"sort":"throughput","allow_fallbacks":true, ...}`，让 OpenRouter 在**同一模型的
  多个 provider** 间自动挑当前不拥堵的那个。仅在 `llm_base_url` 含 `openrouter.ai` 时注入
  （官方/自建端点不认该字段，无条件注入会 400）。可配 `PROVIDER_SORT`（price/throughput/latency）、
  `LLM_PROVIDER_ONLY` / `LLM_PROVIDER_IGNORE`。
- **模型回退链**：`LLM_FALLBACK_MODELS` 声明备用模型序列。`_complete_chain()` 主模型失败即换下一个，
  全部失败才按既有契约降级/上抛；`vision()` 同样走链（视觉主模型被限流 → 视觉备用模型顶上）。
  这比"只重试同一模型"更有效：免费池限流常是**单模型**级，换模型能真正绕开。
- **限流感知退避**：`_backoff()` 对 429/503 解析响应头 `Retry-After` 或错误文中的
  "retry after N seconds"，退避下限抬到 ≥N（封顶 90s），不再 1.5s 猛冲；其他错误仍走轻量退避
  （2s 起）。可配 `LLM_RATE_LIMIT_WAIT_S`。
- **配置解析修复**：`Annotated[list[...], Json()]` —— 必须用 `Json()` **实例**（裸类 `Json` 不匹配
  `isinstance(md, Json)`），否则 pydantic-settings 会把 `.env` 里的逗号分隔串当 JSON 强解并报
  `SettingsError`。配合 `mode="plain"` 校验器，`.env` 既支持 `a,b,c` 也支持 `["a","b"]`。
- 测试：`tests/test_dynamic_routing.py`（19 项：provider 注入/跳过、only/ignore、排序值校验、
  回退链去重/字典形式/单点失败切换/全失败上抛/无回退降级、退避口径、Retry-After 解析、
  CSV 与 JSON 两种配置写法、vision 走链）。
- 配套 `.env` 示例回退链：`google/gemma-4-31b-it:free,nvidia/nemotron-nano-12b-v2-vl:free,
  google/gemma-4-26b-a4b-it:free,minimax/minimax-m3:free`。

### P2-3 实测：真实端到端首次完整跑通（EXIT=0 / FINISH）
修复前首轮即被 429 打死；修复后**同一条真实链路跑完并 FINISH**：

| 指标 | 值 |
|---|---|
| 结果 | `EXIT=0`，`STATUS=FINISH`，`ERROR=None` |
| 阶段 | context / planner×3 / analyst×3 / reflection×3 / reporter 全 **SUCCESS** |
| 模型回退 | **11 次**「主模型 429 → 自动切备用 → 成功」 |
| 请求 | 352 次 429 / 20 次 200（免费共享池真实压力） |
| 单阶段耗时 | 145~187s（被限流拖慢，非代码问题） |

**真实模型自己诊断出了两个缺陷**（reflection 输出）：
`incorrect SQL query used (count instead of sum)`、
`image_analyze failed due to missing image parameter`，并判定 REPLAN。
这本身就是 Reflection/REPLAN 设计的价值：**不给出假结论**。

### P2-3 复测（第二轮）：发现"第一轮修复不够深"

第二轮真实端到端（同一脚本、同一模型 `gemma-4-31b-it:free`）跑完 `EXIT=0 / FINISH`，
但**两个缺陷依然复现**——说明第一轮修复只覆盖了表层。深挖后定位到更根本的原因：

| 症状 | 表层归因（第一轮的判断） | 真实根因（第二轮才看清） |
|---|---|---|
| `image_analyze` FAILED「缺少参数 image」 | 模型发了 `image_path` 而非 `image` | **模型根本没填 `input`**，`input=null`，参数写在 objective 人话里（`"...values of sales_chart.png"`）→ 别名表无用武之地 |
| `sql_query` 结果是 count 不是 sum | 坏 SQL 被丢弃后退回合成 SQL | 丢弃是**对的动作**，但合成 SQL 只会 `COUNT(*)` → 等于"从错的退回到另一种错的"；模型的聚合意图本可救回 |

据此做**第二轮加深修复**：

1. **自然语言兜底（关键）**：`vision_tool` 新增 `_guess_image_from_text()`，用正则从
   objective / action / question 里"捞"带图片扩展名的 token（`.png/.jpg/.jpeg/.gif/.webp/...`），
   优先带引号的完整文件名；再加一层"session 下只有一张图就直接用"的兜底。
   同时 `nodes.build_executor_params` 的 `image_analyze` 分支也做同样回填，并把
   `_objective` / `_action` 透传给工具（双通道，任一層缺失都能救）。
2. **SQL 最小修复而非丢弃**：新增 `_repair_sql_balance()`——逐个删除造成 `depth<0` 的多余
   `)`、末尾按 depth 补齐缺失的 `)`。修好就**保留模型的正确聚合意图**（`SUM("revenue"))`
   → `SUM("revenue")`），修不好才退合成 SQL。引号不配平则不做危险修复、原样返回。
3. **合成 SQL 也贴合分析意图**：兜底分支新增 `_is_numeric_col()`，有数值度量列（revenue /
   amount / units / 营收…）时用 `SUM(...)`，而不是无条件 `COUNT(*)`。
4. **参数别名**（第一轮成果，保留）：`_first_param(params, *aliases)` 接受
   `image / image_path / path / file / image_file / filename / image_name` 等
   （含 `{"image": {"path": ...}}` 嵌套写法），question 同理；支持 session 图片目录内绝对路径直接寻址。

> **真实模型与 Mock 的本质差异**：Mock 永远产出符合 schema 的规范输入，因此
> 「参数名别名」「JSON 形状松散」「SQL 括号写错」这三类问题**只在真实模型下暴露**。
> 更狠的一条：**模型可能连 `input` 都不填**，把参数塞进自然语言——这要求"从人话里抽参数"
> 的能力，是所有 schema 化设计的盲区。
> P2-3 的落地过程印证了一条工程原则：**接真实模型时，"容错入参" 与 "回退链路" 同等重要**，
> 且"容错"要一直容错到**自然语言层**。

### P2-3 修复清单（最终态）

| # | 问题 | 修复手段 | 测试 |
|---|---|---|---|
| 1 | 免费池 429 打死整链 | provider 排序 `sort=throughput` + `allow_fallbacks`（仅 openrouter 注入） | `test_dynamic_routing.py` |
| 2 | 单模型被限流 | `LLM_FALLBACK_MODELS` 模型回退链，逐个顶替 | 同上 |
| 3 | 退避太激进 | 429/503 尊重 `Retry-After`，下限抬高、封顶 90s | 同上（含真 tenacity 集成测试） |
| 4 | 免费 slug 下架 404 | `_is_model_gone()` 识别 + `_gone_models` 永久跳过 | 同上 |
| 4b | **402/401/403 被回退链放大** | `_is_account_level()` → 整链 `break` 快速失败 | `test_retry_policy.py` |
| 5 | 计划 JSON 形状松散 | `PlanModel._coerce` + `_coerce_str_list` 展平 | `test_real_llm_shape_tolerance.py` |
| 6 | 工具参数名不符 | `_first_param()` 别名表（含嵌套 dict） | `test_real_llm_robustness.py` |
| 7 | **参数完全不填、写进人话** | `_guess_image_from_text()` 正则抽文件名 + 唯一附件兜底（**双层**回填） | 同上 |
| 8 | 畸形 SQL（多括号） | `_repair_sql_balance()` 最小修复、**保留聚合意图** | 同上 |
| 9 | 合成 SQL 只会 COUNT | `_is_numeric_col()` → 数值列优先 SUM | 同上 |
| 10 | **并行执行器缺 executor span** | `run_executor_all` 补 `@trace("executor")` | `test_eval_harness.py` |

> 第 7 条是最深的一层：**模型不遵守结构化输出契约**。任何 schema 化设计都要假设
> "模型可能把参数写进自然语言"，并在参数合成层与工具层**各兜一次**。

> 第 10 条是**全量回归抓出来的隐藏缺陷**：P1-1 并行化（`run_executor_all`）只搬了业务逻辑、
> 漏搬 `@trace("executor")` 装饰器 → 主路径 trace 里永远没有 executor span →
> eval 的 `tool_calls` 恒为 0，**可观测性指标静默失真**（不报错、不影响结果，最难发现）。
> 这也解释了真实 e2e 日志为何只有 5 个阶段——那是 bug，不是正常现象。

---

## 四、验证方法

改完 P0 后，用同一份 sleep.csv 重跑，应满足：

1. `schema_search` 能列出 `sleep.csv`（或它的虚拟表名）
2. `dataset_profile` 的样本数 = 400（而非别的表）
3. `build_plan()` 的步骤指向附件，不含内置库表名
4. 报告中出现至少 3 个**具体数值**（如"压力≥8 的人群睡眠质量均值 X.X"）
5. 不再出现"Mock分析师"字样
6. REPLAN 轮次 ≤ 1

建议把这条作为**回归测试**固化下来（`tests/test_attachment_routing.py`），
防止以后改路由逻辑时再次退化。

---

## 五、修复实录（2026-09-10 实施）

实施 P0 时，发现了三个**比原诊断更深一层**的根因。它们解释了为什么"光把附件接上"还不够。

### 根因 A：内存态缓存 vs 文件真相源不一致

`attached_tables()` 只读进程内 dict，而 `attach_clause()` 读文件系统。两者在
**重启后 / 多 worker 下**会分叉：SQL 侧能挂上 `upload.sleep`，schema 侧却报告"没有这张表"。

→ **修复**：`sidecar` 文件成为唯一真相源。`attached_tables()` 缓存未命中时**反射 sidecar**；
sidecar 内新增 `_meta` 表记录源文件名/原始列序，保证跨进程也能还原上下文。

### 根因 B：`sql_query` 丢弃计划里的 SQL

`build_executor_params()` 对 `sql_query` **完全忽略 `step.input.sql`**（只有 `freeform` 消费）。
于是确定性附件计划里精心写好的聚合 SQL 被丢弃，退化成 `SELECT 1` —— 计划"看起来对了"，
执行却是空的。

→ **修复**：`sql_query` 优先消费 `input.sql`；并新增 `_qualify_upload()`，把计划里的裸表名
`FROM sleep` 自动限定为 `FROM upload.sleep`（不动内置库表名）。

### 根因 C：降级路径的 JSON 解析是贪婪正则（REPLAN 空转的直接元凶）

```python
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)   # 旧实现
```

`build_user_message()` 把结构化 payload 放进 `<task_context>`，用户原话放进 `<user_request>`。
但 `MockLLM._extract_json()` 用**贪婪正则**在整段文本上取「第一个 `{` 到最后一个 `}`」——
一旦用户原话或工具输出里出现花括号，整体 `json.loads` 失败 → 返回 `None` →
Mock analyst 看到**空的 `tool_results`** → 恒输出「未获取到可支撑的事实数据」 →
Reflection 恒判 REPLAN → **三轮空转**。

这是「REPLAN 原地打转」真正的机制：不是反思不聪明，而是**降级路径根本没拿到数据**。

→ **修复**：优先解析 `<task_context>` 块；兜底改用**括号配对扫描**（`_balanced_objects()`，
正确处理字符串内的转义与花括号）。

### 端到端验证（同一份 400 行 sleep.csv，同一句提问）

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 编排事件数 | 25 | **10** |
| REPLAN 次数 | 3 | **0** |
| 执行步骤 | `schema_search` → `dataset_profile` → 查内置库 | **`s1_rows` / `s2_by_dim` / `s2b_by_dim2`，全查 `upload.sleep`** |
| 数据来源 | 内置库 100 行无关数据 | **用户上传的 400 行真实数据** |
| 报告发现数 | 0（"未获取到事实数据"） | **3 条，置信度 0.90** |
| 质检结论 | 证据不足（0.50） | **证据充分（0.90）** |

实际产出的数值与案例二人工分析的结论方向一致 —— 例如按职业分组：

```
occupation,n,avg_stress,avg_sleep_quality,avg_sleep_duration
Student,100,6.25,5.72,7.66
Manual Labor,104,5.94,6.22,7.4
Office Worker,100,5.61,5.21,7.55     ← 睡眠质量最低
Retired,96,5.59,6.79,7.65
```

### 仍未闭环的部分

**端到端复现是在 `MOCK_LLM=true` 下完成的。** 当前 `.env` 指向的 OpenRouter 模型返回
`403 This model is not available in your region.`，熔断长期处于 OPEN，真实模型链路
（Planner 自主决策、Analyst 深度推断、Reporter 撰写）**尚未验证**。

三个修复都是在**确定性层**完成的，不依赖 LLM，因此离线验证有效；但"真实模型下
是否会自主选择 `upload.<t>`"仍需在有效凭证下复测。

---

## 六、最终验证结果（真实模型配置下的一次诚实复现）

> 这一节记录 2026-09-10 收尾时，用**真实 LLM 配置**（非 MOCK）跑出的完整结果。
> 它比"全绿"更有价值：它精确划出了「确定性层已闭环」与「模型层未闭环」的边界。

**复现命令**（服务以真实 `.env` 启动，`/health` 显示 `llm_degraded:false`）：

```bash
# 1) 上传附件
POST /api/v1/chat/upload?session_id=sleep-verify   → 400 行解析成功
# 2) 用案例一的原话提问
POST /api/v1/chat/analyze/stream
  {"query":"这份文件反应了什么数据规律","session_id":"sleep-verify","stream":true}
```

**结果一：编排骨架已完全收敛。**

| 指标 | 案例一（修复前） | 本次（修复后，真实模型配置） |
|---|---|---|
| 编排事件总数 | 25 | **10** |
| REPLAN 次数 | 3 | **0** |
| 工具调用次数 | 3（+2 轮重复） | **3，零重复** |
| 执行步骤 | 查内置库 | **`s1_rows`(56ms) / `s2_by_dim`(8ms) / `s2b_by_dim2`(4ms)** |
| 数据来源 | 内置库 100 行 | **`upload.sleep` 400 行** |
| 返回行数 | 100（无关数据） | **1 / 4 / 4（真实聚合）** |
| 报告发现数 | 0 | **3 条** |
| 数值可追溯率 | — | **3/3 = 1.0** |
| `quality_issues` | 非空 | **`[]`（空）** |

关键证据（取自 SSE 日志 `tmp_sleep/sse_verify.log`）：

```
[3] sql_query  step_id=s1_rows      SUCCESS  56ms  → 1 行 → s1_rows.csv
[4] sql_query  step_id=s2_by_dim    SUCCESS   8ms  → 4 行 → s2_by_dim.csv
[5] sql_query  step_id=s2b_by_dim2  SUCCESS   4ms  → 4 行 → s2b_by_dim2.csv
[6] ANALYZE    trace_summary={"numeric_claims":3,"traced_claims":3,"rate":1.0}
[7] REFLECT    quality_issues=[]           ← 质检无异议
[9] FINISH     workflow_progress={done:6,total:6}
```

报告正文已渲染出真实数值：

```
发现 1（置信度 0.90）：{"n_rows": 400}
发现 2（置信度 0.90）：{"occupation":"Student","n":100,"avg_stress":6.25,"avg_sleep_quality":5.72,...}
发现 3（置信度 0.90）：{"bmi":"Overweight","n":118,"avg_stress":6.02}
```

**结论：这三个根因修复是有效的，且在真实服务进程中同样成立（不是只有 pytest 里过）。**

**结果二：模型层暴露了一个新问题 —— 降级静默。**

同一次运行中 `degraded = true`，`llm_fallbacks` 恰好 **5 条**，覆盖全部 5 个 LLM 阶段：

| 阶段 | 错误 |
|---|---|
| `context` | 403 This model is not available in your region. |
| `planner` | 同上 |
| `analyst` | 同上 |
| `reflection` | 同上 |
| `reporter` | 同上 |

这说明两件事：

1. **降级管道本身是健康的** —— 403 之后流程没有崩、没有卡、没有无限重试，
   而是逐级回落到确定性实现，并**仍然产出真实数值**。这正是单元/离线测试之外，
   降级设计的价值所在。
2. **但降级在编排层是静默的** —— 用户在报告里只看到一句
   "当前处于降级模式（未启用真实模型）"，看不到 **究竟哪一级降级了、为什么降级**。
   对排障极不友好。

**→ 新增 P0-4（见下节）**：把 `llm_fallbacks` 提升为一等公民，在报告顶部与前端
都显式暴露「降级链路 + 原因 + 影响范围」。

### 案例一 / 案例二的差距，还剩多少？

| 能力项 | 案例二（人工 Claude） | 当前 Agent（确定性层） | 差距 |
|---|---|---|---|
| 拿到真实数据 | ✅ | ✅ | **已补齐** |
| 按职业/维度分组统计 | ✅ | ✅（occupation + bmi 双维度） | **已补齐** |
| 数值可追溯 | ✅ | ✅ 3/3 | **已补齐** |
| 因果推断（压力→睡眠） | ✅ | ❌ 仅描述 | **待 Analyst 真实模型** |
| U 型曲线识别（7-9h 最优） | ✅ | ❌ | **待 Analyst 真实模型** |
| 风险倍数（肥胖→呼吸暂停 2.3×） | ✅ | ❌ | **待 Analyst 真实模型** |
| 多假设交叉验证 | ✅ | ⚠️ 仅 2 维度 | **待 P1-1 并行化** |
| 自然语言洞察撰写 | ✅ | ⚠️ 模板化 | **待 Reporter 真实模型** |

即：**"数据流通"已经修好，"洞察深度"还差一个能用的模型。**
在拿到有效模型凭证前，继续改代码的边际收益很低 —— 建议先把 P0-4 做掉，
然后换一个可用区域的模型凭证，再跑一次同样的 sleep.csv 复现。

### 回归测试状态

| 测试范围 | 结果 |
|---|---|
| `tests/test_attachment_routing.py`（附件路由专项） | **21 passed** |
| 完整离线套件（排除 `*_live` / `test_agent_real`） | **349 项：0 失败 / 0 错误 / 0 跳过** |
| 收集总数（含需外部依赖的 live 测试） | 376 |

先前出现过的 4 个 `OperationalError: database is locked` 已确认**不是代码缺陷**，
而是两个 pytest 进程并发争抢同一份 sidecar 文件锁。修复：fixture 改用
`uuid.uuid4().hex[:10]` 唯一 session_id，`sqlite3.connect` 加 `busy_timeout`，
并新增 `test_fixture_session_ids_are_isolated` 作为并发隔离回归守卫。

> 一个值得记录的设计反馈：暴露这个问题的，正是本次给 `materialize_table`
> 新增的那行 warning 日志。此前它是静默 `return ""`，故障以"偶发测试失败"的形式
> 出现，极难定位。**这与 P0-4 是同一个病 —— 静默失败是一切问题的根源。**
