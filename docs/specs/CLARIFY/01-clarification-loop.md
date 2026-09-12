# CLARIFY/01 澄清回路 — 规格 v1.0

> 动因：基准 `docs/测试用例.md` 里 38/300 涉口径澄清、19 条是"该不该"的**决策题**。
> 真实分析师遇到模糊请求会**反问一句**；我们现在的行为是——
> `run_context` 命中 `clarification_required` 就 `status=ERROR` + `error="需要澄清: …"` **直接终止**，
> 而且**在 `state.context = ctx` 之前 return**，结构化问题被丢进错误字符串里。
> 这不是"更智能"，这是坏掉。

## 1. 目标
把澄清变成一次**正常的对话回合**：Agent 提问 → 用户回答 → 同一会话继续跑完。

## 2. 后端契约

### 2.1 状态
- `AgentStatus` 增 `"CLARIFY"`（与 `FINISH` 一样是**终止态**：本轮到此结束，等用户回答）。
- `run_context`：**先 `state.context = ctx` 再分支**（问题必须留在结构化字段里），
  `status="CLARIFY"`、`error=None`、`metadata["clarification"] = {"questions": [...], "assumptions": [...], "objective": ...}`。
- `graph._drive_sync` 与 `stream_analysis` 都要在 CLARIFY 处早退（**不**进 planner）。
  CLARIFY 快照同样要经 `_attach_llm_fallbacks`（否则"降级后反问"时降级不可见）。

### 2.2 续跑语义（关键，错了会死循环）
- pending 存 `short_term`（键 `pending_clarification`，**不是 state 字段**——续跑会重建 state）：
  `{"questions": [...], "original_query": ..., "created": ts}`。
- 下一轮请求：若存在 pending，把 `pending_clarification` **与**用户新消息一起注入 context payload
  （`build_user_message` 会自动序列化，装配代码不用改）；成功解析后**清除 pending**。
- `AnalyzeRequest.clarification_answer: Optional[str]`：显式作答（也允许直接把答案放进 `query`）。
- **`resume_analysis` 必须给 CLARIFY 独立语义**：现实现把"非 FINISH"一律当重试 →
  会重跑 Context **再次反问**，形成死循环。新语义：checkpoint 为 CLARIFY 且**未提供新 query** →
  **原样返回该状态**（让调用方重新展示问题），不重跑。

### 2.3 对外
- `AnalyzeResponse.clarification: Optional[dict]`（`{questions, assumptions, objective}`）。
- SSE：CLARIFY 帧带 `clarification`；`_status_message` 增 `"CLARIFY": "需要澄清"`。
- 澄清**不**触发质量门禁/口径检查（它们挂在下游节点上，CLARIFY 在 context 就返回了）——规格显式写明。

### 2.4 提示词（只追加）
`context.md` 追加：若 payload 含 `pending_clarification`，把用户本轮消息视为对它的回答，
合并后**必须**把 `clarification_required` 置回 `false`（除非仍有新的缺口）。
澄清问题**最多 3 个**、必须具体可答（"要含退款还是不含？"而不是"请补充更多信息"）。

## 3. 前端契约（`web/`）
| 文件 | 改动 |
|---|---|
| `src/lib/api.ts` | `AgentEvent.clarification?`、`Clarification` 接口、`stageLabel` 增 CLARIFY、**`isTerminal` 必含 CLARIFY** |
| `src/lib/types.ts` | `Message.clarification?`、`Message.answered?` |
| `src/App.tsx` | `appendEvent` 存 clarification；CLARIFY **不**写入 `error`；作答后清空 clarification |
| `src/components/ClarifyCard.tsx`（新） | 渲染问题清单 + 输入框 + 提交（提交即发下一轮请求） |
| `src/components/ChatMessage.tsx` | 在 PlanCard 旁渲染 `ClarifyCard` |
| `src/components/StageTimeline.tsx` | tone/终态判定增 CLARIFY（否则显示成普通 idle 行） |

> **最容易漏的一处**：`isTerminal` 不含 CLARIFY 时，SSE 结束后不再有新事件，
> 界面会永久显示"正在连接智能体…"转圈。

## 4. 边界
- 与 ROUTE 的关系：`detect_mode` 在 context **之后**执行；CLARIFY 早退 → 不产出 mode/intent（可接受）。
- 与 E3 迭代的关系：澄清时**不**碰 `last_dataset`。
- 澄清轮**不**写 long_term 记忆（没有结论可沉淀）。
- 一次澄清后仍 `clarification_required=true` → 允许（最多连续 2 轮），第 3 轮强制按已有信息推进，
  避免无限反问（`metadata["clarify_rounds"]` 计数）。

## 5. TDD
| 用例 | 断言 |
|---|---|
| 节点 | `clarification_required=true` → `status=CLARIFY`、`error is None`、`context` 已落、`metadata.clarification.questions` 非空 |
| 同步编排 | CLARIFY 时 planner **未**被调用（早退） |
| 流式 | 末帧 `status=CLARIFY` 且带 `degraded` 字段（attach 未被跳过） |
| API | 响应 `clarification.questions` 非空、`status="CLARIFY"` |
| SSE | CLARIFY 帧含 `clarification`，`message` 为"需要澄清" |
| **续跑不循环** | 澄清后带答案再请求 → 跑到 FINISH；pending 被清除 |
| **resume 语义** | CLARIFY checkpoint + 无新 query → **原样返回 CLARIFY**（不重跑、不再次反问） |
| pending 注入 | 续跑时 context payload 含 `pending_clarification` |
| 轮次上限 | 连续 3 轮仍要求澄清 → 第 3 轮按已有信息推进（不无限反问） |
| 前端契约 | `isTerminal` 含 CLARIFY；`stageLabel` 有 CLARIFY（源码级断言，仿 `tests/test_ui.py`）+ `npm run build` 通过 |
