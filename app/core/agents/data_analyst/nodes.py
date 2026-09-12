"""Orchestration nodes for the Data Analyst Agent.

Each node mutates an :class:`AgentState`. The LLM calls go through
:mod:`app.infrastructure.llm.router`; tool execution goes through
:mod:`app.core.tools`. Tool *arguments* are synthesised deterministically from
the plan plus the schema discovered at runtime (``build_executor_params``) – a
deliberate choice for reproducibility and safety ("Be reproducible"). The LLM
owns understanding, planning, analysis, reflection and reporting; the executor
owns turning a step into a concrete, validated tool call.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ....config import get_settings
from ....core.memory import long_term, short_term
from ....core.prompts import build_system_message, build_user_message, load_prompt
from ....core.tools import execute_tool
from ....core.tools.errors import is_schema_error
from ....infrastructure.llm.router import get_llm
from ....infrastructure.observability.tracing import trace
from ...agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    ModelOutputError,
    PlanModel,
    PlanStep,
    ReflectionResult,
    ToolResult,
)

logger = logging.getLogger("da.nodes")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# CLARIFY/01：最多连续反问 2 轮，第 3 轮按已有信息推进（绝不无限反问）
_MAX_CLARIFY_ROUNDS = 2


def _parse_json(text: str) -> dict:
    if not text:
        return {}
    m = _JSON_RE.search(text)
    try:
        return json.loads(m.group(0)) if m else json.loads(text)
    except json.JSONDecodeError:
        return {}


def _llm(stage: str, user: str, json_mode: bool = True) -> str:
    # §21 层级：SYSTEM PROMPT → AGENT ROLE PROMPT → SECURITY CLAUSE；
    # system 消息只由受控模板构成，用户输入永远只出现在 user 消息的数据块里。
    return get_llm().complete(build_system_message(stage), user, stage=stage, json_mode=json_mode)


def _schema_feedback(exc: Exception) -> str:
    """把 pydantic 的字段级错误压成一段可回喂模型的简短文本。"""
    text = str(exc)
    if len(text) > 1200:
        text = text[:1200] + " …（截断）"
    return text


def _llm_model(model_cls, stage: str, user: str, *, ok=None, fallback=None, retries: int = 1):
    """LLM → JSON → pydantic，**输出不可用就把具体原因回喂模型重试**。

    真实（尤其免费/小）模型会产出结构不合法的 JSON。此前 4 处 ``model_validate``
    直接裸抛 ``ValidationError`` → 穿透 ``run_analysis`` → **整个请求/整次 eval 崩掉**
    （实测：一次真实重跑因此丢掉全部 7 条基线）。

    ⚠️ **"能过校验" ≠ "可用"**：这套 schema 的字段几乎都有默认值，加上 ``_coerce``
    的容错，``PlanModel.model_validate({})`` 是**成功**的（空计划）。所以只有 pydantic
    校验不够 —— 调用方要传 ``ok(model) -> bool`` 表达"内容上可接受吗"
    （如"计划至少要有 1 个可用步骤"）。第一次实测就栽在这：只靠校验判成败，
    空计划会被当成"成功"，重试逻辑形同虚设。

    返回 ``(model, err)``：``err is None`` 表示一次通过；否则是降级说明（供披露）。
    ``fallback(parsed, exc)`` 给出降级实例；不给则抛 :class:`ModelOutputError`。
    重试时把**具体原因**回喂 —— 只是原样重试同一个提示词基本无效。
    """
    parsed: dict = {}
    reason = ""
    prompt = user
    for attempt in range(retries + 1):
        raw = _llm(stage, prompt)
        parsed = _parse_json(raw)
        try:
            model = model_cls.model_validate({**parsed, "raw": parsed})
            if ok is None or ok(model):
                return model, None
            reason = ("输出结构合法但**内容不可用**：关键字段为空"
                      f"（首 400 字符：{raw[:400]}）")
        except Exception as exc:  # pydantic ValidationError 及其它解析异常
            reason = _schema_feedback(exc)
        if attempt < retries:
            prompt = (
                f"{user}\n\n## 上一次输出不可用（第 {attempt + 1} 次尝试）\n"
                f"原因：\n{reason}\n\n"
                "请**严格**按要求重新输出**完整**对象：每个必填字段都要给，"
                "不要省略、不要留空、不要输出解释性文字。"
            )
    note = f"{stage} 输出不可用（已重试 {retries} 次）: {reason}"
    if fallback is not None:
        return fallback(parsed, reason), note
    raise ModelOutputError(note)


# --------------------------------------------------------------------------- #
def _last_result(state: AgentState, tool: str) -> ToolResult | None:
    for r in reversed(state.tool_results):
        if r.tool == tool and r.status == "SUCCESS":
            return r
    return None


def _first_table(state: AgentState) -> tuple[str, list[str]]:
    """取「当前应查询的表」。ATTACH/03：上传表优先于内置库。

    用户上传了数据时，任何「自动选表」都必须落在 ``upload.<t>`` 上 ——
    否则 dataset_profile / sql_query 的兜底参数会指向内置企业库。
    """
    res = _last_result(state, "schema_search")
    if res and res.output.get("tables"):
        t = res.output["tables"][0]
        cols = [c["name"] for c in t.get("columns", [])]
        return t["table"], cols
    # schema_search 尚未跑：直接问附件层要（避免首步就落到内置库）
    ups = _uploaded_tables(state)
    if ups:
        return ups[0]["table"], [c["name"] for c in ups[0].get("columns", [])]
    return "", []


def _is_numeric_col(col: str) -> bool:
    """判断列名是否像数值度量列（合成 SQL 的 SUM 目标选择用）。

    只做名字启发式：真实列类型在 ``_first_table`` 返回的 cols 里只有名字，
    没有类型。名字命中典型度量词即认为是数值列 —— 这比无条件 COUNT 更贴合
    "计算营收/销量"这类分析意图。
    """
    c = (col or "").lower()
    return any(
        kw in c
        for kw in ("revenue", "amount", "price", "cost", "sales", "qty", "quantity",
                   "units", "count", "total", "value", "profit", "score", "num",
                   "营收", "金额", "价格", "成本", "销量", "数量", "总额", "利润")
    )


def _qualify_upload(sql: str, state: AgentState) -> str:
    """把 SQL 里对上传表的裸引用补成 ``upload.<table>``。

    计划/模型常写 ``FROM sleep``，但边车库表只在 ``upload`` schema 下可见 ——
    不补前缀就会 `no such table: sleep`。这里只做**表名**限定，
    不改列名、不碰内置库表名（避免误伤 fact_sales 等）。
    """
    if not sql or "upload." in sql.lower():
        return sql
    try:
        ups = _uploaded_tables(state)
    except Exception:
        return sql
    if not ups:
        return sql
    builtin = {"fact_sales", "dim_product", "dim_region", "dim_channel"}
    for t in ups:
        name = t["table"]
        if not name or name in builtin or name.lower() in builtin:
            continue
        # 只在「FROM/JOIN 后紧跟裸表名」的位置替换
        sql = re.sub(rf"(?i)\b(FROM|JOIN)\s+{re.escape(name)}\b",
                     lambda m: f"{m.group(1)} upload.{name}", sql)
    return sql


def _sql_balanced(sql: str) -> bool:
    """粗校验 SQL 括号/引号是否配平（真实模型常写坏 SQL）。

    只做括号与引号计数，不做完整语法解析：够用来拦截
    ``SUM("revenue"))``、``WHERE x IN ("a","b"`` 这类明显畸形，
    避免把坏 SQL 直接发给数据库后报语法错误、再拖垮下游步骤。
    """
    if not sql or not sql.strip():
        return False
    depth = 0
    in_s = in_d = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_s:
            if ch == "'":
                in_s = False
        elif in_d:
            if ch == '"':
                in_d = False
        elif ch == "'":
            in_s = True
        elif ch == '"':
            in_d = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_s and not in_d


def _repair_sql_balance(sql: str) -> str:
    """尽最大努力修掉多余的闭括号 / 补齐缺失的闭括号。

    真实 e2e：gemma 把 ``SUM("revenue")`` 写成 ``SUM("revenue"))`` —— 多一个 ``)``。
    模型的**意图是对的**（按区域 SUM 聚合），只是括号写坏。直接丢弃会退化成
    ``COUNT(*)``（各区域计数=2），等于把"错的"换成"另一种错的"。这里做最小修复：
      1) 逐个删除造成 depth<0 的多余 ``)``；
      2) 末尾按 depth 补齐缺失的 ``)``。
    引号不平衡等无解情况返回原串（由调用方按不合法处理）。
    """
    if not sql or not sql.strip():
        return sql
    out: list[str] = []
    depth = 0
    in_s = in_d = False
    for ch in sql:
        if in_s:
            out.append(ch)
            if ch == "'":
                in_s = False
            continue
        if in_d:
            out.append(ch)
            if ch == '"':
                in_d = False
            continue
        if ch == "'":
            in_s = True
            out.append(ch)
        elif ch == '"':
            in_d = True
            out.append(ch)
        elif ch == "(":
            depth += 1
            out.append(ch)
        elif ch == ")":
            if depth > 0:
                depth -= 1
                out.append(ch)
            # depth == 0 时这个 ')' 是多余的 —— 丢弃
        else:
            out.append(ch)
    if in_s or in_d:
        return sql  # 引号都不配平，无法安全修复
    fixed = "".join(out)
    if depth > 0:
        fixed += ")" * depth
    return fixed


def _adversarial_state(state: AgentState, issues: list) -> Any:
    """E5/02：把对抗性检查结果写进结构化字段（供 API/eval 断言）。"""
    from .state import AdversarialCheck

    codes = {getattr(i, "code", "") for i in issues}
    return AdversarialCheck(
        dq_override_requested="dq_override_requested" in codes
        or "dq_override_silent" in codes,
        refused="dq_override_requested" in codes,
        unmarked_quality_issues=[] if "dq_override_silent" not in codes else ["未披露质量问题"],
        untested_comparisons=["untested_comparison"] if "untested_comparison" in codes else [],
    )


def _semantics_text(state: AgentState) -> str:
    """SEMANTIC/01：业务语义紧凑文本（采集失败/为空 → 空串，绝不影响主流程）。"""
    try:
        from ....core.semantics import collect_semantics, describe_semantics

        return describe_semantics(collect_semantics(getattr(state, "session_id", "")))
    except Exception as exc:
        state.metadata["semantics_error"] = str(exc)
        return ""

def build_executor_params(state: AgentState, step: PlanStep) -> dict[str, Any]:
    """Synthesize validated tool arguments for a plan step from runtime context."""
    tool = step.tool
    ctx = state.context
    if tool == "schema_search":
        kw = (ctx.metrics or ctx.analysis_object or [""])[0]
        return {"keyword": kw}
    if tool == "dataset_profile":
        table, _ = _first_table(state)
        # E4/01：计划步骤可显式声明 key / 日期列 / join 放大对照表 / 待画像 SQL；
        # 缺省按表自动探测（候选键、日期列都是自动的）。标识符与只读校验在工具内。
        inp = getattr(step, "input", None) or {}
        params: dict[str, Any] = {"table": table}
        for name in ("key", "base_table", "date_column", "sql"):
            if inp.get(name):
                params[name] = inp[name]
        return params
    if tool == "sql_query":
        # E2 扩展（ATTACH/03）：计划步骤可直接给出只读 SQL。
        # 此前只有 freeform 消费 input.sql，导致确定性计划里的 SQL 被丢弃、
        # 退化成 `_first_table` 的合成结果（无表时就是 `SELECT 1`）。
        inp_sql = ((getattr(step, "input", None) or {}).get("sql") or "").strip()
        if inp_sql:
            candidate = _qualify_upload(inp_sql, state)
            # ⚠️ 真实模型会把 SQL 写坏：括起来括号不配平、多余 `)` 之类。
            # 实测 gemma 产出 `SUM("revenue")` 时写成 `SUM("revenue"))`，
            # 而 `_qualify_upload` 只会**追加**括号 → 直接语法错误。
            # 注意：模型的**聚合意图往往是对的**，只丢不用会退化成 COUNT(*)，
            # 等于把"错的"换成"另一种错的"。所以先尝试最小修复，修好就用。
            if _sql_balanced(candidate):
                return {"sql": candidate}
            repaired = _repair_sql_balance(candidate)
            if repaired != candidate and _sql_balanced(repaired):
                logger.warning("步骤 %s 的 SQL 括号不配平，已自动修复后使用：%s → %s",
                               getattr(step, "id", "?"), candidate[:160], repaired[:160])
                return {"sql": repaired}
            logger.warning("步骤 %s 的 input.sql 无法修复，已丢弃并改用合成 SQL：%s",
                           getattr(step, "id", "?"), candidate[:200])
        table, cols = _first_table(state)
        if not table:
            return {"sql": "SELECT 1"}
        dim = next((d for d in ctx.dimensions if d in cols), None)
        # 合成 SQL 也要尽量贴合"分析"意图：有数值列就 SUM，而不是一味 COUNT。
        # （真实 e2e 曾因退化到 COUNT 让"各区域营收"变成"各区域行数=2"。）
        num = next(
            (c for c in cols if c not in (dim,) and _is_numeric_col(c)),
            None,
        )
        if dim and num:
            return {"sql": f'SELECT "{dim}", SUM("{num}") AS total_{num} FROM {table} '
                           f'GROUP BY "{dim}" ORDER BY total_{num} DESC LIMIT 20'}
        if dim:
            return {"sql": f'SELECT "{dim}", COUNT(*) AS cnt FROM {table} '
                           f'GROUP BY "{dim}" ORDER BY cnt DESC LIMIT 20'}
        return {"sql": f"SELECT * FROM {table} LIMIT 100"}
    if tool == "freeform":
        # E2：模型在计划步骤 input.sql 里直接给出只读 SQL（守卫在 sql_tool 内）
        # E7/01：input.source 指定命名数据源（缺省 = 主源）
        sql = ((step.input or {}).get("sql") or "").strip()
        src = (getattr(step, "input", None) or {}).get("source")
        if sql:
            return {"sql": sql, **({"source": src} if src else {})}
        return {"sql": "SELECT 1"}  # 空 → 无效占位，交由只读守卫判空
    if tool == "python_analysis":
        sql_res = _last_result(state, "sql_query")
        csv_path = sql_res.output.get("csv_path") if sql_res else None
        code = (
            "summary = {'rows': 0, 'cols': [], 'numeric_summary': {}}\n"
            "if df is not None:\n"
            "    summary = {\n"
            "        'rows': int(df.shape[0]),\n"
            "        'cols': list(df.columns),\n"
            "        'numeric_summary': df.describe().to_dict(),\n"
            "    }\n"
            "print(_json.dumps(summary, default=str))"
        )
        return {"code": code, "data_csv": csv_path} if csv_path else {"code": code}
    if tool == "visualization":
        res = _last_result(state, "sql_query")
        rows = res.output.get("rows", []) if res else []
        cols = list(rows[0].keys()) if rows else []
        x = cols[0] if cols else None
        y = cols[1] if len(cols) > 1 else cols[0]
        return {"chart_type": "bar", "x": x, "y": y, "data": rows[:50],
                "title": ctx.objective[:30] or "chart"}
    if tool == "knowledge_search":
        return {"query": ctx.objective, "top_k": 4}
    if tool == "generate_report":
        return {"analysis": state.analysis.model_dump(),
                "reflection": state.reflection.model_dump() if state.reflection else None,
                "objective": ctx.objective}
    if tool == "image_analyze":
        # P2-2：从计划步骤 input 里取出图片文件名与问题（与 sql_query/freeform
        # 消费 input 一致），同时透传 session_id 供工具定位落地图片。
        #
        # ⚠️ 真实模型（gemma）实测：它**不填 input**，而是把参数塞进人话 objective
        #    （"Read ... sales_chart.png"），input=null → 工具直接判「缺少参数 image」。
        #    这里做第一层回填：objective/action 里捞文件名；捞不到就看输入里是否
        #    只有一张图（唯一图片直接用）。
        inp = getattr(step, "input", None) or {}
        image = inp.get("image") or inp.get("image_path") or inp.get("path") or ""
        question = inp.get("question") or inp.get("query") or ""
        if not image:
            from ...tools.vision_tool import _guess_image_from_text
            image = _guess_image_from_text(
                str(getattr(step, "objective", "") or ""),
                str(getattr(step, "action", "") or ""),
                str(inp.get("objective") or ""),
            )
        if not image:
            try:
                from ...attachments import attached_images
                imgs = attached_images(state.session_id)
                if len(imgs) == 1:
                    image = imgs[0].get("name") or imgs[0].get("path") or ""
            except Exception:
                pass
        if not question:
            question = str(getattr(step, "objective", "") or "") or "提取图中的关键数据、指标与趋势"
        return {
            "image": image,
            "question": question,
            "_objective": getattr(step, "objective", "") or "",
            "_action": getattr(step, "action", "") or "",
        }
    return {}


# --------------------------------------------------------------------------- #
# Context Resolver
# --------------------------------------------------------------------------- #
@trace("context")
def run_context(state: AgentState) -> AgentState:
    state.status = "UNDERSTAND"

    # 短期记忆召回；超限历史先做滚动摘要（05 记忆 Q8：有界不丢早期硬约束）
    short_mem = short_term.get_all(state.session_id)
    hist = list(short_mem.get("history") or [])
    try:
        from ....core.memory.summarize import condense_history
        recent, summary = condense_history(hist)
        if summary:
            short_mem["history"] = recent
            short_mem["history_summary"] = summary
    except Exception:
        pass  # 记忆故障绝不打断主流水线（§20）

    # 长期记忆召回 + 文本字段截断（上下文预算）
    long_hits = long_term.search(state.user_query, top_k=3)
    try:
        from ....core.memory.budget import truncate
        long_hits = [{**h, "text": truncate(h.get("text", ""), 500)} for h in long_hits]
    except Exception:
        pass

    # P1-3 闭环：把历史写回的「降级教训」读回来，让本轮 planner/analyst 知道
    # 该租户 LLM 曾降级（如区域限制），从而主动依赖确定性分析、并提示用户修凭证。
    # best-effort：读通道故障绝不能打断主流水线（§20）。
    try:
        deg_lessons = [l for l in long_term.recent_lessons(limit=5)
                       if l.get("category") == "llm_degradation"]
    except Exception:
        deg_lessons = []

    # CLARIFY/01：上一轮是否在等用户回答某个澄清问题
    pending = short_term.get(state.session_id, "pending_clarification")
    prior_rounds = int(short_term.get(state.session_id, "clarify_rounds", 0) or 0)
    attempt = prior_rounds + 1

    payload = {
        "user_query": state.user_query,
        "conversation_history": state.conversation_history,
        "available_data_sources": [get_settings().data_db_url],
        # Memory 召回（§20）：短期=本会话工作上下文，长期=跨会话已验证的业务结论
        "short_term_memory": short_mem,
        "long_term_memory": long_hits,
        # P1-3：历史上写回的降级教训（如「该模型区域不可用」），供本轮规避
        "known_degradation_risks": deg_lessons,
    }
    if pending:
        # 让模型知道"用户这句话是在回答什么"，否则会把答案当新问题
        payload["pending_clarification"] = {
            "questions": pending.get("questions"),
            "original_query": pending.get("original_query"),
        }
    # SEMANTIC/01：把"业务词 → 列"的线索给 context（"华东"→region_id）
    sem_text = _semantics_text(state)
    if sem_text:
        payload["business_semantics"] = sem_text
    # §21：用户原话进入 <user_request> 数据块，结构化附加上下文进入 <task_context>
    # 校验失败：回喂错误重试一次；仍失败则降级为空 Context（后续按原话推进），
    # 并把降级写进 state.error —— 绝不让一个阶段的畸形输出打挂整条链路。
    ctx, ctx_err = _llm_model(
        ContextModel, "context", build_user_message(state.user_query, payload),
        # 可用性：得有目标，或明确要走澄清（澄清回合 objective 可为空）。
        ok=lambda c: bool(c.objective) or c.clarification_required,
        # 降级用 raw=... 构造（ContextModel 各字段都有默认值，不会二次抛错）
        fallback=lambda parsed, _exc: ContextModel(raw=parsed or {}),
    )
    if ctx_err:
        state.error = f"context 解析降级: {ctx_err}"
    # **先落盘再分支**：旧实现在赋值前就 return，结构化问题被丢进 error 字符串
    state.context = ctx
    state.metadata["clarify_rounds"] = attempt

    questions = [q for q in (ctx.clarification_questions or []) if q]
    if ctx.clarification_required and questions and attempt <= _MAX_CLARIFY_ROUNDS:
        state.status = "CLARIFY"
        state.error = None  # 澄清不是错误
        state.metadata["clarification"] = {
            "questions": questions,
            "assumptions": list(ctx.assumptions or []),
            "objective": ctx.objective,
        }
        short_term.put(state.session_id, "pending_clarification", {
            "questions": questions,
            "original_query": (pending or {}).get("original_query") or state.user_query,
            "created": time.time(),
        })
        short_term.put(state.session_id, "clarify_rounds", attempt)
        return state

    if ctx.clarification_required and questions:
        # 连续反问到上限：按已有假设推进，并留下披露（绝不无限反问）
        state.metadata["clarify_forced"] = questions
        if ctx.assumptions:
            state.context.assumptions = list(ctx.assumptions)

    # 解析成功 → 清除待澄清状态
    short_term.put(state.session_id, "pending_clarification", None)
    short_term.put(state.session_id, "clarify_rounds", 0)
    return state


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
def _uploaded_tables(state: AgentState) -> list[dict[str, Any]]:
    """取该 session 已落地的上传表（含列名），供 Planner 定位真实数据源。

    走 ``attached_tables``（sidecar 为真相源），所以重启/多 worker 也能拿到。
    """
    try:
        from ....core.attachments import attached_tables
        return attached_tables(getattr(state, "session_id", "") or "")
    except Exception:
        return []


def _attachment_plan(state: AgentState, uploaded: list[dict[str, Any]]) -> PlanModel:
    """附件存在时的**确定性计划**：必须先在 upload.<t> 上取真数，再做分析。

    ATTACH/03：这不是「抢走 LLM 的活」，而是**保底**。降级/小模型经常忽略
    自然语言里的上传提示，或产不出可执行的 SQL；此时一段确定性的、语法正确、
    指向用户真实数据的计划，比让循环空转三轮 REPLAN 强得多。
    """
    t = uploaded[0]["table"]
    cols = [c["name"] for c in uploaded[0].get("columns", [])]
    low_cols = {c.lower(): c for c in cols}
    # 维度/指标按「解释力」优先级排序，而不是照列顺序 —— 否则会出现
    # 「按 gender 分组、只看 age」这种信息量很低的计划。
    dim_priority = ("occupation", "bmi", "sleep_disorder", "gender")
    metric_priority = ("stress", "sleep_quality", "sleep_duration", "physical_activity")
    _non_metric = {"person_id", "id", "gender", "occupation", "bmi", "sleep_disorder"}
    dim_cols = [low_cols[k] for k in dim_priority if k in low_cols]
    num_cols = [low_cols[k] for k in metric_priority if k in low_cols]
    # 补充：优先表里其余未被列出的字段
    num_cols += [c for c in cols if c not in _non_metric and c not in num_cols]
    steps: list[dict[str, Any]] = [
        {
            "id": "s1_rows",
            "objective": f"确认 upload.{t} 的数据规模",
            "action": "统计上传表总行数",
            "tool": "sql_query",
            "input": {"sql": f"SELECT COUNT(*) AS n_rows FROM upload.{t}"},
            "dependencies": [],
            "expected_output": "一行统计结果",
        },
    ]
    if dim_cols and num_cols:
        dim = dim_cols[0]
        metrics = ", ".join(
            f"ROUND(AVG(CAST({c} AS REAL)), 2) AS avg_{c}" for c in num_cols[:3]
        )
        steps.append({
            "id": "s2_by_dim",
            "objective": f"对比 {dim} 各组的 {', '.join(num_cols[:3])} 差异",
            "action": f"按 {dim} 分组聚合",
            "tool": "sql_query",
            "input": {"sql": (f"SELECT {dim}, COUNT(*) AS n, {metrics} "
                              f"FROM upload.{t} GROUP BY {dim} ORDER BY avg_{num_cols[0]} DESC")},
            "dependencies": ["s1_rows"],
            "expected_output": "各组均值对比表",
        })
        # 第二维度（若存在）：单维度对比容易漏掉交互效应，补一条交叉验证
        extra_dims = [d for d in dim_cols[1:] if d != dim]
        if extra_dims:
            d2 = extra_dims[0]
            m1 = num_cols[0]
            steps.append({
                "id": "s2b_by_dim2",
                "objective": f"换一个维度 {d2} 复核 {m1} 的组间差异是否稳健",
                "action": f"按 {d2} 分组聚合",
                "tool": "sql_query",
                "input": {"sql": (f"SELECT {d2}, COUNT(*) AS n, "
                                  f"ROUND(AVG(CAST({m1} AS REAL)), 2) AS avg_{m1} "
                                  f"FROM upload.{t} GROUP BY {d2} ORDER BY avg_{m1} DESC")},
                "dependencies": ["s1_rows"],
                "expected_output": "第二维度对比表",
            })
    elif num_cols:
        metrics = ", ".join(
            f"ROUND(AVG(CAST({c} AS REAL)), 2) AS avg_{c}" for c in num_cols[:5]
        )
        steps.append({
            "id": "s2_overall",
            "objective": f"建立 upload.{t} 的数值基线",
            "action": "计算主要字段总体均值",
            "tool": "sql_query",
            "input": {"sql": f"SELECT {metrics} FROM upload.{t}"},
            "dependencies": ["s1_rows"],
            "expected_output": "一行基线指标",
        })
    steps.append({
        "id": "s3_profile",
        "objective": f"刻画 upload.{t} 的分布与异常",
        "action": "对上传表做画像",
        "tool": "dataset_profile",
        "input": {"table": t},
        "dependencies": ["s1_rows"],
        "expected_output": "分布/异常画像",
    })
    return PlanModel.model_validate({
        "goal": "基于用户上传数据的探索性分析",
        "stopping_criteria": ["上传表的规模、组间差异、分布画像均有真实数值支撑"],
        "risk_points": ["上传表可能含脏值（如 130/85），数值列需 CAST"],
        "steps": steps,
        "raw": {"source": "attachment_fallback"},
    })


@trace("planner")
def run_planner(state: AgentState) -> AgentState:
    state.status = "PLAN"
    task_context: dict[str, Any] = state.context.model_dump()
    task_context["mode"] = getattr(state, "mode", "full")  # ROUTE：输出意图
    # SEMANTIC/01：planner 此前**完全看不到 schema**（只有 context+mode）。
    # 业务语义（维表取值 + 按命名约定的关系）让它规划 join 时有据可依，而不是凭猜键。
    sem_text = _semantics_text(state)
    if sem_text:
        task_context["business_semantics"] = sem_text

    # INTERVIEW/01 ③：工具多时按语义路由（工具少时全给——路由是负收益）。
    # 只注入被选中的工具名，避免 100 个工具的说明淹没 prompt。
    try:
        from ....core.tools.routing import select_tools_for_planner

        tool_names, routed = select_tools_for_planner(
            state.user_query, threshold_count=get_settings().tool_routing_threshold)
        if routed:
            task_context["available_tools"] = tool_names
            state.metadata["routed_tools"] = tool_names
            state.metadata["tool_routing"] = {"routed": True, "selected": len(tool_names)}
    except Exception as exc:  # 路由故障不得打断规划（回退全量工具说明）
        state.metadata["tool_routing_error"] = str(exc)
    # ATTACH/03：把「用户上传了哪些可查询表」显式喂给 Planner。
    # 只靠 user_query 里那段自然语言提示，模型（尤其降级/小模型）容易忽略，
    # 转而去查内置企业库 —— 这正是「传了 sleep.csv 却分析 fact_sales」的根因。
    # 这里给的是**结构化**字段，比散文提示更难被忽略。
    uploaded = _uploaded_tables(state)
    if uploaded:
        task_context["uploaded_datasets"] = uploaded
        task_context["data_source_priority"] = (
            "用户已上传数据，必须优先且只使用 upload.<table> 作为数据源；"
            "内置企业库（fact_sales / dim_* 等）与本轮问题无关，禁止查询。"
        )
    # P2-2：把「用户上传了哪些图片」作为结构化字段喂给 Planner（与 uploaded_datasets 平行）。
    # 图片不走边车库，而是落地原图、由 image_analyze 工具读取；
    # 同样用结构化字段而非散文，避免模型忽略多模态输入。
    try:
        from ....core.attachments import attached_images
        imgs = attached_images(getattr(state, "session_id", "") or "")
    except Exception:
        imgs = []
    if imgs:
        task_context["uploaded_images"] = imgs
        task_context.setdefault("data_source_priority", "")
        task_context["data_source_priority"] += (
            "；用户还上传了图片，若问题与图中数据/图表相关，"
            "必须用 image_analyze 工具实际读取图片（不要只凭文件名臆测）。"
        )
    # 错误回注 REPLAN 回路：重规划时把"为什么失败/缺什么证据"回注给 Planner，
    # 让新计划针对缺口调整（换字段/换表/换工具），而不是原样重跑（§23 + DeepAnalyze 式自纠错）。
    if state.replan_count > 0 and state.reflection is not None:
        failed = [
            {
                "tool": r.tool,
                "step_id": r.step_id,
                "error": r.error,
                "error_class": r.error_class,
            }
            for r in state.tool_results
            if r.status != "SUCCESS" and r.error
        ]
        task_context["previous_attempt"] = {
            "replan_round": state.replan_count,
            "replan_objectives": state.reflection.replan_objectives,
            "missing_evidence": state.reflection.missing_evidence,
            "reflection_summary": state.reflection.summary,
            "failed_tools": failed,
            "instruction": (
                "上一轮分析证据不足。请针对上述缺口制定新计划："
                "修正失败步骤的根因（换表/换字段/换工具），补齐缺失证据，不要原样重复旧步骤。"
            ),
        }
    # 无 fallback：计划是必须的。重试后仍不可用 → ModelOutputError，
    # 由编排层转成 status=ERROR（而不是裸抛 ValidationError 打挂整跑）。
    # 注意"可用"要显式判定：字段都有默认值，空计划也能过 pydantic 校验。
    plan, plan_err = _llm_model(
        PlanModel, "planner", build_user_message(state.user_query, task_context),
        ok=lambda p: bool(p.steps),
    )
    if plan_err:
        state.metadata["plan_parse_degraded"] = plan_err
    # ATTACH/03 保底：用户上传了数据，但模型产出的计划**完全没碰** upload.<t>
    # （降级模式、小模型忽略提示、或直接去查内置库）→ 用确定性附件计划替换。
    # 这是打破「查内置库 → 无证据 → REPLAN」空转循环的关键。
    if uploaded and not _plan_uses_upload(plan, uploaded):
        plan = _attachment_plan(state, uploaded)
    # 真实 LLM 可能产出超过上限的步骤数；在此截断，让 max_plan_steps 配置对
    # 真实计划同样生效（此前只对 Mock 计划截断）。
    max_steps = get_settings().max_plan_steps
    if len(plan.steps) > max_steps:
        plan.steps = plan.steps[:max_steps]
    state.plan = plan
    state.current_step_index = 0
    return state


def _plan_uses_upload(plan: PlanModel, uploaded: list[dict[str, Any]]) -> bool:
    """计划里是否至少有一个步骤真正引用了用户上传的表。"""
    tables = {t["table"] for t in uploaded}
    for s in plan.steps:
        blob = " ".join(str(x) for x in (
            getattr(s, "objective", "") or "",
            getattr(s, "action", "") or "",
            json.dumps(getattr(s, "input", None) or {}, ensure_ascii=False, default=str),
        ))
        low = blob.lower()
        if "upload." in low:
            return True
        if any(t and t.lower() in low for t in tables):
            return True
    return False


# --------------------------------------------------------------------------- #
# Executor (advances one step per call)
# --------------------------------------------------------------------------- #
@trace("executor")
def run_executor(state: AgentState) -> AgentState:
    state.status = "EXECUTE"
    steps = state.plan.steps
    if state.current_step_index >= len(steps):
        state.status = "ANALYZE"
        return state
    step = steps[state.current_step_index]
    deps_ok = all(_dep_done(d, state) for d in step.dependencies)
    if not deps_ok:
        result = ToolResult(step_id=step.id, tool=step.tool, status="FAILED",
                            error="依赖步骤未完成")
    else:
        params = build_executor_params(state, step)
        result = execute_tool(step.id, step.tool, params, state.session_id)
        # §23 确定性分支路由：Invalid Column/Table → Schema Search → Retry。
        # 不等一整轮 REPLAN，立即用最新 schema 重建参数并重试一次（有界）。
        if (result.status == "FAILED" and is_schema_error(result.error)
                and not step.id.endswith("__retry")):
            recovery = execute_tool(f"{step.id}__recovery_schema", "schema_search",
                                    {"keyword": (state.context.metrics or [""])[0]},
                                    state.session_id)
            state.tool_results.append(result)
            state.tool_results.append(recovery)
            if recovery.status == "SUCCESS":
                retry_params = build_executor_params(state, step)
                result = execute_tool(f"{step.id}__retry", step.tool, retry_params, state.session_id)
    state.tool_results.append(result)
    if result.status == "SUCCESS":
        try:
            from ....core.agents.data_analyst.iteration import save_last_dataset
            save_last_dataset(state)  # E3：会话数据集，供下一轮增量迭代
        except Exception:
            pass
    state.current_step_index += 1
    if state.current_step_index >= len(steps):
        state.status = "ANALYZE"
    return state


def _dep_done(dep_id: str, state: AgentState) -> bool:
    return any(r.step_id == dep_id and r.status == "SUCCESS" for r in state.tool_results)


def _execute_one_step(step: PlanStep, state: AgentState) -> list[ToolResult]:
    """P1-1：执行单个步骤并返回其 ToolResult 列表（**不**修改共享 state）。

    与 ``run_executor`` 的逐步骤逻辑一一对应，但改为「纯函数式」返回，
    以便放进线程池并发执行：同一波次的步骤彼此依赖已满足，不会读写对方的
    ``tool_results``，故并发安全；合并回 ``state.tool_results`` 由主线程在波次
    结束后统一做（避免多线程写同一 list）。
    """
    deps_ok = all(_dep_done(d, state) for d in step.dependencies)
    if not deps_ok:
        return [ToolResult(step_id=step.id, tool=step.tool, status="FAILED",
                           error="依赖步骤未完成")]
    params = build_executor_params(state, step)
    result = execute_tool(step.id, step.tool, params, state.session_id)
    # §23 确定性分支路由：Invalid Column/Table → Schema Search → Retry。
    results: list[ToolResult] = [result]
    if (result.status == "FAILED" and is_schema_error(result.error)
            and not step.id.endswith("__retry")):
        recovery = execute_tool(f"{step.id}__recovery_schema", "schema_search",
                                {"keyword": (state.context.metrics or [""])[0]},
                                state.session_id)
        results.append(recovery)
        if recovery.status == "SUCCESS":
            retry_params = build_executor_params(state, step)
            results.append(execute_tool(f"{step.id}__retry", step.tool, retry_params, state.session_id))
    return results


def _run_batch(steps: list[PlanStep], state: AgentState, max_workers: int) -> dict[str, list[ToolResult]]:
    """并发执行一批互相独立的步骤；返回 {step_id: [ToolResult, ...]}。"""
    if len(steps) <= 1 or max_workers <= 1:
        return {s.id: _execute_one_step(s, state) for s in steps}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out: dict[str, list[ToolResult]] = {}
    with ThreadPoolExecutor(max_workers=min(len(steps), max_workers)) as ex:
        futures = {ex.submit(_execute_one_step, s, state): s for s in steps}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                out[s.id] = fut.result()
            except Exception as exc:  # 单步异常绝不拖垮整波
                out[s.id] = [ToolResult(step_id=s.id, tool=s.tool,
                                        status="FAILED", error=str(exc)[:300])]
    return out


@trace("executor")
def run_executor_all(state: AgentState) -> AgentState:
    """P1-1：按依赖关系把计划拆成「波次」，同一波内并发执行，跨波次仍串行。

    * 波次划分 = 经典拓扑分层：第 N 波的依赖都已在第 <N 波完成。
    * 天然兼容原顺序语义：仅含 1 个独立步骤时退化为一步一执行。
    * 若某步失败导致其后续步骤永远「依赖未完成」，剩余步骤打 FAILED 占位，
      与顺序执行器的容错行为一致（不卡死、不漏步）。
    * 失败/恢复产生的 ``llm_fallbacks`` 由 router 自身记录，本函数不触碰。

    ⚠️ 必须带 ``@trace("executor")``：主路径（``run_analysis`` → 本函数）若缺
    span，eval 的 ``tool_calls``（= 数 trace 里的 executor span）会恒为 0，
    可观测性指标失真。此前只有顺序版 ``run_executor`` 有装饰器，并行版漏了。
    """
    state.status = "EXECUTE"
    steps = state.plan.steps
    if not steps:
        state.status = "ANALYZE"
        return state
    done_ids: set[str] = {r.step_id for r in state.tool_results}
    max_workers = get_settings().parallel_executor_workers
    safety = 0
    while len(done_ids) < len(steps) and safety < len(steps) + 2:
        safety += 1
        ready = [s for s in steps if s.id not in done_ids
                 and all(_dep_done(d, state) for d in s.dependencies)]
        if not ready:
            # 依赖失败 → 给剩余步骤打 FAILED 占位，避免无限循环
            for s in steps:
                if s.id not in done_ids:
                    state.tool_results.append(
                        ToolResult(step_id=s.id, tool=s.tool, status="FAILED",
                                   error="依赖步骤未完成"))
                    done_ids.add(s.id)
            break
        batch = _run_batch(ready, state, max_workers)
        for step in ready:
            for r in batch.get(step.id, []):
                state.tool_results.append(r)
            done_ids.add(step.id)
            if any(r.status == "SUCCESS" for r in batch.get(step.id, [])):
                try:
                    from ....core.agents.data_analyst.iteration import save_last_dataset
                    save_last_dataset(state)  # E3：会话数据集，供下一轮增量迭代
                except Exception:
                    pass
    state.current_step_index = len(steps)
    state.status = "ANALYZE"
    return state


# --------------------------------------------------------------------------- #
# Analyst
# --------------------------------------------------------------------------- #
@trace("analyst")
def run_analyst(state: AgentState) -> AgentState:
    state.status = "ANALYZE"
    payload = {
        "context": state.context.model_dump(),
        "plan": state.plan.model_dump(),
        "tool_results": [r.model_dump() for r in state.tool_results],
    }
    # SEMANTIC/01：结论要写"华东"而不是"区域 1"
    sem_text = _semantics_text(state)
    if sem_text:
        payload["business_semantics"] = sem_text
    # 校验失败回喂重试一次；仍失败则降级为只带 raw 的空分析（**绝不崩**）。
    state.analysis, ana_err = _llm_model(
        AnalysisResult, "analyst", build_user_message(state.user_query, payload),
        fallback=lambda parsed, _exc: AnalysisResult(raw=parsed or {}),
    )
    if ana_err:
        state.error = f"analyst 解析降级: {ana_err}"
    # E1 溯源：数值型 evidence 若缺 sql_id，自动补最近成功 SQL 的 step_id（有源才补）
    try:
        from ....core.agents.data_analyst.sources import auto_trace
        auto_trace(state.analysis.findings, state.tool_results)
    except Exception:
        pass  # 溯源后处理不得打断流水线
    state.status = "REFLECT"
    return state


# --------------------------------------------------------------------------- #
# Reflection (quality gate)
# --------------------------------------------------------------------------- #
@trace("reflection")
def run_reflection(state: AgentState) -> AgentState:
    state.status = "REFLECT"
    payload = {
        "context": state.context.model_dump(),
        "analysis": state.analysis.model_dump(),
        "tool_results": [r.model_dump() for r in state.tool_results],
    }
    # 校验失败回喂重试一次；仍失败则用 ReflectionResult 的默认值（decision=REPLAN，
    # 是"保守"的一侧：宁可再审一轮也不放过没证据的结论）并留下降级说明。
    refl, refl_err = _llm_model(
        ReflectionResult, "reflection", build_user_message(state.user_query, payload),
        fallback=lambda parsed, _exc: ReflectionResult(raw=parsed or {}),
    )
    if refl_err:
        state.metadata["reflection_parse_degraded"] = refl_err

    # E4/04 质量门禁：确定性检查 LLM 看不到的"数据事实"（join 放大 / 主键不唯一 /
    # 日期稀疏 / 粒度误读），**只收紧决策**，并把披露文本写进 analysis.quality_notes。
    gate_issues: list = []
    try:
        from .gate import apply_gate, is_blocked, profile_gate
        from ....config import get_settings as _settings

        _s = _settings()
        gate_issues = profile_gate(state.tool_results, state.analysis,
                                   amp_threshold=_s.profile_join_amp_threshold,
                                   null_ratio=_s.profile_null_high_ratio)
        # E5/01 + E5/02：统计声明缺失 + 对抗性指令（与数据质量共用同一套 GateIssue/apply_gate）
        from .rigor import adversarial_issues, dq_disclosure_note, stats_issues

        gate_issues = list(gate_issues) + stats_issues(state.analysis) +             adversarial_issues(state.user_query, state.analysis)
        override_note = dq_disclosure_note(state.user_query, state.analysis)
        if override_note and override_note not in (state.analysis.quality_notes or []):
            state.analysis.quality_notes = list(state.analysis.quality_notes or []) + [override_note]
        refl.adversarial = _adversarial_state(state, gate_issues)
        decision, notes = apply_gate(gate_issues, refl)
        if notes:
            state.analysis.quality_notes = notes
        if decision != refl.decision:
            refl.decision = decision
        state.metadata["gate_issues"] = [i.model_dump() for i in gate_issues]
        state.metadata["gate_block"] = is_blocked(gate_issues)
    except Exception as exc:  # 门禁故障绝不能打断主流程
        state.metadata["gate_error"] = str(exc)

    # E4/03 口径可比性：结构性判定由确定性规则给（LLM 的语义判读为补充，标 [待真实验证]）
    try:
        from .caliber import apply_caliber, caliber_check, caliber_notes

        check = caliber_check(state.analysis, state.context,
                              iteration=state.iteration, report=state.report or "")
        refl.caliber_comparability = check
        if check.issues:
            state.analysis.quality_notes = list(state.analysis.quality_notes or []) +                 caliber_notes(check)
        cal_decision = apply_caliber(check, refl)
        if cal_decision != refl.decision:
            refl.decision = cal_decision
        state.metadata["caliber_issues"] = [i.model_dump() for i in check.issues]
    except Exception as exc:  # 口径检查故障绝不打断主流程
        state.metadata["caliber_error"] = str(exc)

    state.reflection = refl
    if refl.decision == "PASS":
        state.status = "REPORT"
    elif refl.decision == "FAIL":
        state.status = "FAILED"
        state.error = refl.summary
    else:  # REPLAN
        if state.replan_count >= state.max_replans:
            if state.metadata.get("gate_block"):
                # BLOCK：结论按当前数据必然错（如放大后的求和）。
                # 注意：graph 随后仍会 run_reporter 兜底并把 status 置回 FINISH（既有行为），
                # 调用方据 error + quality_issues(severity=BLOCK) + 报告披露段识别。
                state.status = "FAILED"
                state.error = "质量门禁：存在必须修正的数据质量问题（" + \
                    "；".join(i.detail for i in gate_issues if i.severity == "BLOCK")[:300] + "）"
            else:
                state.status = "REPORT"  # best-effort report with current evidence
        else:
            state.replan_count += 1
            state.status = "REPLAN"
            if gate_issues:
                # 门禁缺口喂给 planner；目标文本必须带唯一描述，否则会撞上
                # graph 的"无进展检测"（同目标连发两次 REPLAN → FAILED）
                refl.replan_objectives = list(refl.replan_objectives or []) + \
                    [f"质量门禁：{i.detail}" for i in gate_issues if i.severity in ("REPLAN", "BLOCK")]
    return state


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #
def _report_evidence(state: AgentState, max_rows: int = 12) -> list[dict[str, Any]]:
    """抽取可写进报告的**真实数字**（而非让模型凭记忆编）。

    REP/01：只挑"有结果集"的成功步骤（sql_query / freeform / dataset_profile），
    带上 SQL 原文 + 列名 + 前若干行，让 Reporter 能引用具体数值。
    """
    out: list[dict[str, Any]] = []
    for r in state.tool_results:
        if r.status != "SUCCESS":
            continue
        if r.tool not in ("sql_query", "freeform", "dataset_profile", "python_analysis"):
            continue
        o = r.output or {}
        rows = o.get("rows") or []
        # 去掉上下文预算哨兵行
        rows = [x for x in rows if isinstance(x, dict) and "_truncated" not in x]
        item: dict[str, Any] = {
            "step": r.step_id,
            "tool": r.tool,
            "row_count": o.get("row_count", len(rows)),
        }
        if (r.input or {}).get("sql"):
            item["sql"] = str(r.input["sql"])[:600]
        if rows:
            item["columns"] = list(rows[0].keys())
            item["rows"] = rows[:max_rows]
        elif o.get("profile"):
            item["profile"] = o["profile"]
        if o.get("csv_path"):
            item["csv"] = str(o["csv_path"])
        out.append(item)
    return out


@trace("reporter")
def run_reporter(state: AgentState) -> AgentState:
    state.status = "REPORT"
    from ....core.tools.report_tool import run as report_run
    tpl = report_run({
        "analysis": state.analysis.model_dump(),
        "reflection": state.reflection.model_dump() if state.reflection else None,
        "objective": state.context.objective,
    })
    if get_settings().use_mock_llm or not tpl.get("report"):
        state.report = tpl.get("report", "")
    else:
        # REP/01：报告必须能引用**原始证据**，而不只是 AnalysisResult。
        # 此前只传 analysis，模型看不到任何真实数字 → 只能输出"未显式计算指标"。
        evidence = _report_evidence(state)
        raw = _llm("reporter", json.dumps({
            "objective": state.context.objective,
            "analysis": state.analysis.model_dump(),
            "evidence": evidence,
            "attachment": state.attachment_context,
        }, ensure_ascii=False, default=str), json_mode=False)
        # MockLLM._stage_reporter 返回 ``{"__markdown__": True}`` 这个"信号"，
        # 但没有任何节点消费它——若直接当报告发出，前端会原样渲染 JSON 字符串。
        # 这里显式检测：信号或非字符串 / 空 → 退回模板报告（仍可渲染完整 Markdown）。
        if not raw or not isinstance(raw, str):
            state.report = tpl.get("report", "")
        else:
            stripped = raw.strip()
            is_signal = stripped.startswith("{") and ("__markdown__" in stripped or '"__markdown__"' in stripped)
            if is_signal:
                state.report = tpl.get("report", "")
            else:
                state.report = raw
    # E1：报告追加「数字来源」块（每条数值 evidence → [src: step_id]）
    try:
        from ....core.agents.data_analyst.sources import append_citations
        state.report = append_citations(state.report or "", state.analysis, state.tool_results)
    except Exception:
        pass
    # P0-4：降级链路可见化 —— 把「哪一级降了 / 为什么 / 影响什么」置顶写进报告。
    # 此前只有一句笼统的"处于降级模式"，用户无从判断结论可信度的边界在哪。
    try:
        state.report = _prepend_degradation_block(state, state.report or "")
    except Exception:
        pass
    state.status = "FINISH"
    _persist_memory(state)
    return state


def _prepend_degradation_block(state: AgentState, report: str) -> str:
    """在报告顶部插入「运行健康度」区块。无降级时原样返回。

    注意：**不能**依赖 ``state.metadata["llm_fallbacks"]``。graph 里的
    ``_attach_llm_fallbacks`` 是在 ``run_reporter`` **之后**才调用的，
    reporter 执行期间 metadata 里还没有事件。所以这里直接向 router 取
    「本轮」事件（按 run_id 归因），同时把摘要回写 metadata 供 API 复用。
    """
    from ....infrastructure.llm.degradation import render_degradation_block, summarize_degradation

    events = (state.metadata or {}).get("llm_fallbacks")
    if not events:
        try:
            from ....infrastructure.llm.router import fallback_events

            events = fallback_events(run_id=state.session_id)
        except Exception:
            events = []

    summary = summarize_degradation(events)
    block = render_degradation_block(summary)
    if not block:
        return report
    # 摘要回写 metadata，供 API/前端直接消费（不必二次解析事件流）
    state.metadata["degradation_summary"] = summary
    # 插在 H1 标题之后（标题下、正文前），标题仍是最顶部元素。
    # 用正则而非字符串前缀：标题里的冒号可能是半角/全角，硬拼字符串会静默不匹配，
    # 结果把区块插到标题**之前**。
    m = re.match(r"\A(#[^\n]*\n+)", report)
    if m:
        return report[:m.end()] + block + "\n\n" + report[m.end():]
    return block + "\n\n" + report


def _persist_memory(state: AgentState) -> None:
    """Terminal-node persistence (§20). Memory must never break the pipeline."""
    try:
        short_term.put(state.session_id, "last_analysis", {
            "objective": state.context.objective,
            "status": state.status,
            "findings": [f.finding for f in state.analysis.findings[:10]],
            "reflection": state.reflection.decision.value if state.reflection else None,
        })
        history = short_term.get(state.session_id, "history", []) or []
        history.append({
            "query": state.user_query[:200],
            "objective": state.context.objective,
            "status": state.status,
        })
        short_term.put(state.session_id, "history", history[-20:])  # 有界滑动窗口
        # 长期记忆只记已完成的非敏感结论（发现陈述文本，不落工具原始数据）
        if state.status == "FINISH" and state.analysis.findings:
            long_term.append({
                "type": "analysis_summary",
                "session_id": state.session_id,
                "objective": state.context.objective,
                "findings": [f.finding for f in state.analysis.findings[:10]],
                "confidence": state.reflection.confidence if state.reflection else None,
            })
        # Reflexion 式教训回流（§20 长期记忆「可复用分析方法论」）：失败 / 低质 /
        # **降级**分析把**结构化、可操作**的教训写回长期记忆。
        #
        # P1-3（关键缺口）：原实现只覆盖 ``state.error`` / reflection FAIL。
        # 但 sleep.csv 案例里 LLM 全程降级到 Mock、报告却以 FINISH + 置信度 0.90
        # 收场 —— 这种「静默失败」从未被记录，下一次同样的会话只会重蹈覆辙。
        # 这里把降级也当作教训写回，并复用 P0-4 的 ``classify_error`` 给出
        # **可操作的规避动作**（如「更换可用区域的模型」），而不只是堆错误串。
        degraded = bool((state.metadata or {}).get("llm_fallbacks"))
        refl_bad = state.reflection and state.reflection.decision in {"FAIL"}
        error_reason = (state.error or "")[:300]
        missing = (state.reflection.missing_evidence[:3]
                   if state.reflection else []) or []
        if error_reason or missing or degraded:
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            lessons: list[dict] = []
            if error_reason or missing:
                lessons.append({
                    "type": "lesson",
                    "category": "execution_failure",
                    "ts": ts,
                    "objective": state.context.objective[:150],
                    "reason": error_reason,
                    "missing_evidence": missing,
                    "reflection_summary": (state.reflection.summary[:200]
                                           if state.reflection else ""),
                })
            if degraded:
                # 降级教训：逐阶段分类根因 + 可操作动作（复用 P0-4 分类器）
                for st in _degradation_summary_for(state).get("stages", []):
                    lessons.append({
                        "type": "lesson",
                        "category": "llm_degradation",
                        "ts": ts,
                        "objective": state.context.objective[:150],
                        "stage": st.get("stage"),
                        "kind": st.get("kind"),          # region_blocked / auth_failed ...
                        "summary": st.get("summary"),
                        "action": st.get("action"),      # 可操作：更换可用区域模型等
                        "impact": st.get("impact"),
                    })
            for lesson in lessons:
                long_term.append(lesson)
                sess = short_term.get(state.session_id, "lessons", []) or []
                sess.append({k: v for k, v in lesson.items() if k != "type"})
                short_term.put(state.session_id, "lessons", sess[-5:])
    except Exception as exc:  # pragma: no cover - memory is best-effort
        logging.getLogger("da.memory").warning("memory persist failed: %s", exc)


def _degradation_summary_for(state: AgentState) -> dict:
    """取本轮降级摘要（与 P0-4 同源）。失败就返回空，绝不让记忆写回中断。"""
    try:
        from ....infrastructure.llm.degradation import summarize_degradation

        events = (state.metadata or {}).get("llm_fallbacks")
        if not events:
            from ....infrastructure.llm.router import fallback_events

            events = fallback_events(run_id=state.session_id)
        return summarize_degradation(events)
    except Exception:
        return {}
