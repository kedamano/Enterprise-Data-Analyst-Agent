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
from typing import Any, Iterator

from ....config import get_settings
from ....core.memory import long_term, short_term
from ....core.prompts import build_system_message, build_user_message, load_prompt
from ....core.tools import execute_tool
from ....core.tools.errors import is_schema_error
from .sql_precheck import dialect_hints, format_error, known_schema
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

# CONTEXT/01：Context 阶段能塞进提示词的表清单上限（防用户上传过多表撑爆预算）
_MAX_TABLES_IN_CONTEXT = 60
_MAX_COLS_PER_TABLE = 40


# 常见"包装壳"的键：只在这些键构成整个对象时，才认为内层才是真 payload
_WRAPPER_KEYS = frozenset({"role", "content", "result", "output", "data",
                           "name", "type", "finish_reason", "tool_call_id"})
_NESTED_CONTENT_KEYS = ("content", "result", "output", "data")


def _unwrap_wrapper(parsed: Any) -> Any:
    """拆掉 `{role, content:"<json>"}` 这类**包装壳**，取出真正的 payload。

    真实模型实测会把整个答复再包一层，且**内层是字符串**：

        {"role": "analyst", "content": "{\\"metrics\\":[],\\"findings\\":[...]}"}

    原实现只解析外层 → 拿到 `{"role","content"}` → `AnalysisResult` 全空 →
    **内容被静默丢掉**（真实基线 7 条用例全部 `findings=0` 就是这么来的）。

    只在该对象**键集合很窄**（⊆ `_WRAPPER_KEYS`）时才拆，避免误伤正常 payload；
    工具调用形状（`{tool, arguments}`）**不拆**——它不是壳，是要被判为不可用的内容。
    """
    for _ in range(3):                     # 有界，防畸形自嵌套
        if not isinstance(parsed, dict):
            return parsed
        if not set(parsed) or not set(parsed) <= _WRAPPER_KEYS:
            return parsed
        inner: Any = None
        for key in _NESTED_CONTENT_KEYS:
            value = parsed.get(key)
            if isinstance(value, dict):
                inner = value
                break
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    inner = decoded
                    break
        if inner is None:
            return parsed
        parsed = inner
    return parsed


def _parse_json(text: str) -> dict:
    if not text:
        return {}
    m = _JSON_RE.search(text)
    try:
        parsed = json.loads(m.group(0)) if m else json.loads(text)
    except json.JSONDecodeError:
        return {}
    return _unwrap_wrapper(parsed)


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


_ANALYSIS_SUBSTANTIVE_FIELDS = ("findings", "metrics", "recommendations",
                                "limitations", "stats_notes")


def _analysis_usable(model: Any) -> bool:
    """`AnalysisResult` 在**内容层面**可用吗。

    ⚠️ 「能过 pydantic 校验」在这里等于废话：`AnalysisResult` 的字段**全有默认值**，
    任何 dict 都能校验通过并得到一份**全空的**分析。真实模型实测正是这么干的
    （`analysis.raw` 出现过五种形状：正确 / `{role,content}` 壳 / 工具调用 /
    别的阶段的 schema / ToolResult dump）。

    后果是**无声的**：`findings=0` → E1 溯源维度 `numeric_claims=0/0` **测不出来**，
    报告也没有任何发现，而流水线一路 FINISH。

    判据：至少有一个实质字段非空。**"如实报告没数据"是合法结论**
    （只有 `limitations` 也算可用），不能被当成不可用反复重试。
    """
    return any(getattr(model, f, None) for f in _ANALYSIS_SUBSTANTIVE_FIELDS)


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
        # 空内容必须**单独说清楚**：此时 `parsed == {}`，而本套 schema 字段几乎都有默认值，
        # `model_validate({})` 会**成功**，于是落到下面那句"结构合法但内容不可用"——
        # 掩盖了真正的原因（模型根本没吐正文）。实测 glm-5.3 在 planner 的大提示词下必现：
        # 它是推理模型，max_tokens 被 reasoning 占满后正文为空串。
        if not (raw or "").strip():
            reason = ("模型返回**空内容**（不是 JSON 不合法，而是没有输出正文）。"
                      "推理模型常见：max_tokens 被 reasoning 占满后正文为空。"
                      f"当前 LLM_MAX_TOKENS={get_settings().llm_max_tokens}，"
                      "请调大后重试（或换非推理模型）")
        else:
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


# 一张表要能支撑"趋势/对比"这类分析，至少得有这么多行。
# 3~8 行的维表（dim_channel / dim_region）在结构上就答不了分析问题。
_MIN_ROWS_FOR_ANALYSIS = 100


def _intent_tokens(text: str) -> set[str]:
    """从意图文本里取**ASCII 词**（首字符为字母、长度 ≥3）。

    只取 ASCII 是刻意的：中文词对英文 schema 零信息量（"营收"不在
    `fact_sales` 的任何名字里），却会在中文表名/列名的库上**偶然命中**，
    那属于运气而不是判断。宁可少一个信号，也不要一个噪声信号。
    """
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text or "")}


def _looks_like_dimension(name: str) -> bool:
    """表名像不像维表。**只作相对信号**（见 `_pick_table`）——命名是惯例不是契约。"""
    n = (name or "").lower()
    return (n.startswith("dim_") or n.endswith("_dim") or n.endswith("_ref")
            or "lookup" in n)


def _measure_columns(cols: list[str]) -> list[str]:
    return [c for c in cols if _is_numeric_col(c)]


def _match_dimension_column(dimensions: list[str], cols: list[str]) -> Optional[str]:
    """把 context 里的维度名映射到**真实列名**。

    两段：① 精确相等（既有行为，优先）；② 否则按「列名以 ``{d}_`` 开头 /
    以 ``_{d}`` 结尾」匹配。

    第二段补的是真实基线的又一处退化：`context.dimensions == ["region"]`
    而列名是 `region_id` —— 精确列表成员判定**永不成立** → `dim=None`
    → 合成 SQL 从分组聚合退化成 ``SELECT *``。
    """
    for d in dimensions:
        if d in cols:
            return d
    for d in dimensions:
        dl = (d or "").lower()
        if not dl:
            continue
        for c in cols:
            cl = c.lower()
            if cl.startswith(dl + "_") or cl.endswith("_" + dl):
                return c
    return None


def _table_candidates(state: AgentState) -> list[dict[str, Any]]:
    """候选表：最近一次成功的 schema_search 结果；没有则退回上传表。"""
    res = _last_result(state, "schema_search")
    if res and res.output.get("tables"):
        return list(res.output["tables"])
    # 退回附件层时补上 `match` 标记：`schema_tool` 会给上传表打这个标记，
    # 直接问附件层拿到的没有——不补，`_pick_table` 就认不出"这是用户自己的数据"。
    out: list[dict[str, Any]] = []
    for t in _uploaded_tables(state):
        t = dict(t)
        t.setdefault("match", "user_upload")
        out.append(t)
    return out


def _pick_table(state: AgentState, extra_text: str = "") -> tuple[str, list[str]]:
    """按**内容**选「当前应查询的表」。E2/02：绝不盲取 `tables[0]`。

    动因（`eval --mode real` 首次全量基线）：`_first_table` 取
    ``schema_search.tables[0]``，而那是 ``insp.get_table_names()`` 的**字典序第一张**
    ——两个样例库里都是 ``dim_channel``（3 行维表）。于是 planner 每步 `input={}`
    时，**每个 SQL 步骤都被合成到同一条** ``SELECT * FROM dim_channel LIMIT 100``，
    返回 3 行、不报错、记 SUCCESS（`工具成功率 0.986`），而报告据此**编出一整张
    区域营收表**还判 ✅。

    打分（确定性，分数相同取候选序在前者）：

    | 信号 | 分 | 理由 |
    |---|---|---|
    | 意图词命中表名/列名 | +3 | 问题里说了 region，就该优先含 region 的表 |
    | 存在度量列 | +2 | 没有可聚合的度量列**答不了分析问题**——维表与事实表的本质差别 |
    | `row_count >= _MIN_ROWS_FOR_ANALYSIS` | +1 | 3 行的表支撑不了"趋势/对比" |
    | 名字像维表（**且有别的正分候选**） | −3 | 相对信号，避免把名为 `dim_x` 的可用表一并压掉 |

    **胜者分 ≤ 0 且候选 ≥2 → `("", [])`** → 调用方合成出空 SQL → 该步**响亮 FAILED**。
    "拿不到一张能用的表"必须表现为失败，而不是"随便找一张表凑一句 SELECT *"。

    边界：候选**只有一张**时**不** fail-closed——没有第二个选项就没有"选错"，
    照用（详见下方 `len(cands) == 1` 分支的回归说明）。
    """
    cands = _table_candidates(state)
    if not cands:
        return "", []
    # ATTACH/01：用户上传了数据时，候选**收缩到上传表**——这是产品政策
    # （run_planner 的 data_source_priority 同一条），不是启发式打分。
    uploads = [t for t in cands if t.get("match") == "user_upload"]
    if uploads:
        cands = uploads

    intent = " ".join([
        extra_text or "",
        getattr(state, "user_query", "") or "",
        getattr(state.context, "objective", "") or "",
        " ".join(getattr(state.context, "metrics", None) or []),
        " ".join(getattr(state.context, "dimensions", None) or []),
    ])
    tokens = _intent_tokens(intent)

    scores: list[int] = []
    for t in cands:
        cols = [str(c.get("name") or "") for c in (t.get("columns") or [])]
        blob = (str(t.get("table") or "") + " " + " ".join(cols)).lower()
        score = 0
        if tokens and any(tok in blob for tok in tokens):
            score += 3
        if not cols:
            # **列清单缺失 ≠ 没有度量列**：这是"无从判断"，不是"判断为否"。
            # 真跑不会出现（`schema_search` 一律带 `insp.get_columns`），
            # 但把"不知道"当成"不合格"会让一个本可用的候选直接判死。
            # 给 +1 使其**可用但排在已知可用的表之后**——它不会盖过
            # 任何有度量列（+2）或词面命中（+3）的候选，也就不会复活
            # "盲取 tables[0]" 那个 bug。
            score += 1
        elif _measure_columns(cols):
            score += 2
        rc = t.get("row_count")
        if isinstance(rc, int) and rc >= _MIN_ROWS_FOR_ANALYSIS:
            score += 1
        scores.append(score)

    best = max(scores)
    idx = scores.index(best)
    # 维表惩罚是**相对**的：只有存在别的候选可挑时才扣。没有别的候选时，
    # `dim_x` 仍是唯一可用表——宁可给出真实的维表数据，也不虚构一张"更合适"的表。
    if best > 0 and len(cands) > 1:
        adjusted = [s - 3 if _looks_like_dimension(t.get("table")) else s
                    for s, t in zip(scores, cands)]
        best = max(adjusted)
        idx = adjusted.index(best)

    if best <= 0 and not uploads:
        # **只有一个候选时不适用 fail-closed**：那说明库里就这一张表，
        # "该查哪张表"**不成为选择**——与上传表优先是同一个道理
        # （用户/环境只给了一份数据，没有选错的可能）。
        #
        # 这一条不是给打分开后门，是**回归逼出来的**：`tests/test_export.py`
        # 的夹具建的是单表 `c(id INTEGER, phone TEXT)`（无度量列），
        # 全是文本/ID 列。旧 `_first_table` 盲取它、流程正常；一刀切
        # fail-closed 会让"导出脱敏值"这类**本就不需要聚合列**的场景整条断掉。
        # 而 D54 的 bug 需要**≥2 个候选**（字典序第一张 vs 真正该查的那张）
        # 才复现——单候选场景没有"选错"这回事。
        if len(cands) == 1:
            t = cands[0]
            return (t.get("table") or "",
                    [str(c.get("name") or "") for c in (t.get("columns") or [])])
        logger.warning("候选表 %s 没有一个能支撑分析（均无度量列/词面无关），"
                       "不合成 SQL", [t.get("table") for t in cands])
        return "", []
    if best <= 0 and uploads:
        # 用户只有这一份数据，"该查哪张表"不成为选择 → 取候选序第一张
        idx = 0
    t = cands[idx]
    return t.get("table") or "", [str(c.get("name") or "") for c in (t.get("columns") or [])]


def _first_table(state: AgentState, extra_text: str = "") -> tuple[str, list[str]]:
    """取「当前应查询的表」。ATTACH/03：上传表优先于内置库。

    用户上传了数据时，任何「自动选表」都必须落在 ``upload.<t>`` 上 ——
    否则 dataset_profile / sql_query 的兜底参数会指向内置企业库。

    E2/02 起**委托** :func:`_pick_table`：保留旧签名与全部调用点，
    但不再等于 ``tables[0]``（见 `_pick_table` 的动因）。
    """
    return _pick_table(state, extra_text)


def _table_source(state: AgentState, table: str) -> str:
    """选中表所在的**命名源**（E7/02）。

    `schema_search` 跨源发现会给每张表带 `source` 标记；选中哪张表，
    SQL 就必须路由到哪个源——否则合成出的 `SELECT ... FROM aoa_dept`
    会打到主源上报 `no such table`。

    返回 ""＝主源（不传 source，行为与旧版完全一致）。
    """
    if not table:
        return ""
    for t in _table_candidates(state):
        if t.get("table") == table:
            src = str(t.get("source") or "").strip()
            if src and src != "default":
                return src
            return ""
    return ""


def _explicit_source(state: AgentState) -> str:
    """用户/计划里**显式点名**的命名源（如「oasys 数据源」「在 crm 里查」）。

    与跨源发现（`_table_source` 依赖 schema_search 命中并返回 `source` 标记）互补：
    当用户直接在问题里说出源名时，这是「要去哪个库」最硬的信号，**不依赖**
    schema 发现是否成功（发现可能因主源先返回表、或 planner 用空 keyword 列全表
    而被跳过）。命中的是 ``available_sources()`` 里的真实源名（排除 default），
    按词边界匹配，避免把普通词误判成源名。

    返回 ""＝未点名（沿用「不传 = 主源」的既有行为）。
    """
    hay = " ".join([
        str(getattr(state.context, "objective", "") or ""),
        str(getattr(state, "user_query", "") or ""),
    ]).lower()
    if not hay:
        return ""
    try:
        from ...tools.datasource import available_sources
        names = [n for n in available_sources() if n and n != "default"]
    except Exception:
        return ""
    if not names:
        return ""
    for name in names:
        if re.search(rf"(?i)(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", hay):
            return name
    return ""


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
                   # E2/02：演示分析库的度量列名（fact_orders.gmv / fact_traffic.visits）
                   "gmv", "visits",
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


_MAX_SCHEMA_TABLES_IN_PROMPT = 12


def _discovered_schema_text(state: AgentState) -> str:
    """最近一次成功 `schema_search` 的**紧凑 schema 摘要**（表名/列名/行数）。

    E2/02：planner 此前**完全看不到 schema**——`business_semantics` 只给维表取值，
    没有任何列清单。于是它**即便想写 `input.sql` 也无从写起**，
    只能产出 `input={}` 的步骤（真实基线里每一步都是）。重规划轮次里
    `schema_search` 的结果已经躺在 `tool_results` 里，这里把它交回给 planner。

    **只给结构，绝不夹带数据行**（脱敏纪律：schema 是结构，取值是数据），
    且条数有界（超出只取前 N 张，避免把 prompt 预算吃光）。
    """
    res = _last_result(state, "schema_search")
    if not res or not res.output.get("tables"):
        return ""
    lines: list[str] = []
    for t in res.output["tables"][:_MAX_SCHEMA_TABLES_IN_PROMPT]:
        cols = [str(c.get("name") or "") for c in (t.get("columns") or [])]
        rc = t.get("row_count")
        suffix = f"  rows≈{rc}" if isinstance(rc, int) else ""
        # E7/02：非主源的表必须标注来源——planner 据此在 input.source 里显式指名，
        # 否则它写出的 SQL 会被默认路由到主源（no such table）。
        src = str(t.get("source") or "").strip()
        if src and src != "default":
            suffix = f"[source={src}]{suffix}"
        lines.append(f"- {t.get('table')}({', '.join(cols)}){suffix}")
    return "\n".join(lines)


# E2/04：源太多时只列前 N 个（prompt 预算有界；主源排在最前，见 `sources()` 的顺序）
_MAX_SOURCES_IN_PROMPT = 8


def _dialect_text(state: AgentState) -> str:
    """配置里**真实的**引擎 → planner 的 SQL 方言先验（E2/04）。

    动因：D54 的真实基线里，模型每一轮都先按 Postgres 习惯写出 `DATE_TRUNC`，
    被预检拦下、REPLAN、重写。预检（`sql_precheck.dialect_hints`）治的是**已发生的**；
    这里治的是**别一上来就写错**——一句先验就够，成本极低。

    **三条硬约束**（少一条都会把护栏变成故障源）：

    1. **绝不回 DSN**：只取 `dialect` 与**源名**（`available_sources()` 的既有约定：
       "只有名字，绝不回 DSN"）。连接串进 prompt 等于把凭据写进模型上下文。
    2. **绝不抛**：读配置失败 → `""` → 调用方不注入键，planner 照常工作。
    3. **有界**：`_MAX_SOURCES_IN_PROMPT` 上限。

    **绝不硬编码 "SQLite"**：E7 多源下 `input.source` 可能指向真 PG 库，那里
    `DATE_TRUNC` 是**原生**写法；写死禁令就会禁止模型用它能用的写法，
    而预检那边根本不拦（按引擎分级）→ **两边口径相反**。引擎一律从配置读。
    """
    try:
        from ....core.tools.datasource import (
            DEFAULT_SOURCE, available_sources, resolve_source)
        from .sql_precheck import dialect_brief

        lines: list[str] = []
        for name in list(available_sources())[:_MAX_SOURCES_IN_PROMPT]:
            try:
                _url, dialect = resolve_source(name)
            except Exception:
                continue  # 单个源解析不了不影响其他源（更不该影响 planner）
            brief = dialect_brief(dialect)
            if not brief:
                continue  # 引擎判不出来 → 这一源**不说**（不猜）
            label = "（主源，未指定 input.source 时用它）" if name == DEFAULT_SOURCE else ""
            lines.append(f"- {name}{label}: {brief}")
        return "\n".join(lines)
    except Exception as exc:
        state.metadata["dialect_error"] = str(exc)
        return ""


def _available_tables(state: AgentState) -> list[dict[str, Any]]:
    """CONTEXT/01：Context 阶段可见的表清单（表名 + 行数 + 列名），实测约 0.03s。

    为什么需要：此前 payload 只给 `data_db_url` 这个连接串，模型**看不见库里有什么表**，
    面对「统计用户表中的总记录数」只能一律反问（实测这种无歧义查询也误触发 CLARIFY）。
    有了清单，模型能自己判断「用户表不存在」并直接如实回答。

    采集失败/为空 → 返回空列表，绝不影响主流程（与 _semantics_text 同款降级策略）。
    """
    try:
        from ....core.tools import schema_tool

        res = schema_tool.run({})
    except BaseException as exc:  # 沙箱删除守卫可能抛 SystemExit，不能只吞 Exception
        state.metadata["schema_inventory_error"] = str(exc)
        return []
    if not isinstance(res, dict) or not res.get("ok"):
        return []
    out: list[dict[str, Any]] = []
    for t in (res.get("tables") or [])[:_MAX_TABLES_IN_CONTEXT]:
        name = t.get("table")
        if not name:
            continue
        cols = [c.get("name") for c in (t.get("columns") or []) if c.get("name")]
        out.append({"table": name, "rows": t.get("row_count"),
                    "columns": cols[:_MAX_COLS_PER_TABLE]})
    return out


def _semantics_text(state: AgentState) -> str:
    """SEMANTIC/01：业务语义紧凑文本（采集失败/为空 → 空串，绝不影响主流程）。"""
    try:
        from ....core.semantics import collect_semantics, describe_semantics

        return describe_semantics(collect_semantics(getattr(state, "session_id", "")))
    except Exception as exc:
        state.metadata["semantics_error"] = str(exc)
        return ""

# 合成 SQL 前**必须先知道表**的工具（拿不到表就合成不出真实查询）
_TABLE_DEPENDENT_TOOLS = ("sql_query", "freeform", "dataset_profile")


def _step_supplies_own_source(step: PlanStep) -> bool:
    """步骤自带表/SQL → 不需要先发现 schema。"""
    inp = getattr(step, "input", None) or {}
    if getattr(step, "tool", "") == "dataset_profile":
        return bool(inp.get("table") or inp.get("sql"))
    return bool(inp.get("sql"))


def _has_schema_result(state: AgentState) -> bool:
    """`schema_search` 是否已经**成功跑过**（不论选表结果如何）。"""
    res = _last_result(state, "schema_search")
    return bool(res and res.output.get("tables"))


def _needs_schema_discovery(step: PlanStep, state: AgentState) -> bool:
    """这一步是否需要**先补一次 schema 发现**才跑得动。

    E2/02：判据从「`_first_table` 解析出了表」改为「`schema_search` 已经跑过」。
    两者在正常情况下等价，但**在"跑过却选不出可用表"时不等价**——
    那种情况下再补一次发现只会拿到同一批候选（白跑一趟，还多一条工具调用），
    正确行为是**让这一步响亮失败**，把"这批候选撑不起分析"如实报上去。
    """
    if getattr(step, "tool", "") not in _TABLE_DEPENDENT_TOOLS:
        return False
    if _has_schema_result(state) or _first_table(state)[0]:
        return False          # 已发现过 schema，或能解析到上传表
    return not _step_supplies_own_source(step)


def ensure_schema_discovered(state: AgentState, steps: list[PlanStep]) -> bool:
    """计划漏排 `schema_search` 时**补跑一次真实的发现**，返回是否真的补了。

    **动因（真实基线实测）**：planner 既不给 `input.sql`、也常漏排 `schema_search`
    → `_first_table` 无表可解析 → 合成不出 SQL → 该步失败 → 后面整串
    `依赖步骤未完成`。真实基线上**工具成功率只有 0.36，失败的全部是这一类**。

    **为什么不改提示词**：`planner.md` 被 `test_prompt_negative` 钉死，且模型未必听。
    改在执行器**把"不可用"变成"可用"**——确定性、可测、不碰提示词。

    补的是**真实工具调用**：进 `tool_results`（可审计），并在 `metadata` 留痕（铁律 3）。
    调用方需保证在主线程调用（并发批次里多线程写 `tool_results` 会串）。
    """
    todo = [s for s in steps if _needs_schema_discovery(s, state)]
    if not todo:
        return False
    # **每次运行只补一次**：若补了却没拿到表（关键词没命中 / 库不可达 / 工具失败），
    # `_first_table` 仍为空 → 下一步、下一波会**再次触发**，5 步的 5 次 sql_query
    # 就是 5 次白跑。实测（test_parallel_executor）确实复现过 s1/s2/s4 各补一次。
    if state.metadata.get("auto_schema_search_done"):
        return False
    state.metadata["auto_schema_search_done"] = True
    keyword = (state.context.metrics or state.context.analysis_object or [""])[0]
    step_id = f"{todo[0].id}__auto_schema"
    # E7/02：用户点名了某个源（如「oasys 数据源」）→ 发现直接打到该源，
    # 否则主源先返回表会把跨源发现整个跳过，命名源里的表就发现不了。
    src = _explicit_source(state)
    search_params = {"keyword": keyword}
    if src:
        search_params["source"] = src
    res = execute_tool(step_id, "schema_search", search_params, state.session_id)
    state.tool_results.append(res)
    state.metadata.setdefault("auto_schema_search", []).append(
        {"step": todo[0].id, "keyword": keyword, "status": res.status,
         "triggered_by": [s.id for s in todo]})
    logger.warning("计划缺少 schema 发现步骤，已自动补跑 schema_search"
                   "（keyword=%r, status=%s）触发步骤：%s",
                   keyword, res.status, [s.id for s in todo])
    return True


def build_executor_params(state: AgentState, step: PlanStep) -> dict[str, Any]:
    """Synthesize validated tool arguments for a plan step from runtime context."""
    tool = step.tool
    ctx = state.context
    if tool == "schema_search":
        kw = (ctx.metrics or ctx.analysis_object or [""])[0]
        src = _explicit_source(state)
        return {"keyword": kw, **({"source": src} if src else {})}
    if tool == "dataset_profile":
        table, _ = _first_table(state)
        # E4/01：计划步骤可显式声明 key / 日期列 / join 放大对照表 / 待画像 SQL；
        # 缺省按表自动探测（候选键、日期列都是自动的）。标识符与只读校验在工具内。
        inp = getattr(step, "input", None) or {}
        params: dict[str, Any] = {"table": table}
        # E7/02：表来自命名源（schema_search 跨源发现）→ 画像路由到同一源
        src = inp.get("source") or _table_source(state, table) or _explicit_source(state)
        if src:
            params["source"] = src
        for name in ("key", "base_table", "date_column", "sql"):
            if inp.get(name):
                params[name] = inp[name]
        return params
    if tool == "sql_query":
        # E2 扩展（ATTACH/03）：计划步骤可直接给出只读 SQL。
        # 此前只有 freeform 消费 input.sql，导致确定性计划里的 SQL 被丢弃、
        # 退化成 `_first_table` 的合成结果（无表时就是 `SELECT 1`）。
        # E7/02：input.source 与 freeform 同权——计划显式指定源时不能丢。
        inp = getattr(step, "input", None) or {}
        inp_src = inp.get("source")
        inp_sql = (inp.get("sql") or "").strip()
        if inp_sql:
            candidate = _qualify_upload(inp_sql, state)
            # ⚠️ 真实模型会把 SQL 写坏：括起来括号不配平、多余 `)` 之类。
            # 实测 gemma 产出 `SUM("revenue")` 时写成 `SUM("revenue"))`，
            # 而 `_qualify_upload` 只会**追加**括号 → 直接语法错误。
            # 注意：模型的**聚合意图往往是对的**，只丢不用会退化成 COUNT(*)，
            # 等于把"错的"换成"另一种错的"。所以先尝试最小修复，修好就用。
            src_kw = {"source": inp_src} if inp_src else (
                {"source": _explicit_source(state)} if _explicit_source(state) else {})
            if _sql_balanced(candidate):
                return {"sql": candidate, **src_kw}
            repaired = _repair_sql_balance(candidate)
            if repaired != candidate and _sql_balanced(repaired):
                logger.warning("步骤 %s 的 SQL 括号不配平，已自动修复后使用：%s → %s",
                               getattr(step, "id", "?"), candidate[:160], repaired[:160])
                return {"sql": repaired, **src_kw}
            logger.warning("步骤 %s 的 input.sql 无法修复，已丢弃并改用合成 SQL：%s",
                           getattr(step, "id", "?"), candidate[:200])
        table, cols = _first_table(state)
        if not table:
            # ⚠️ 这里曾返回 `SELECT 1` —— 它是**合法只读 SQL**，会照常执行、返回 1 行、
            # 让该步记 SUCCESS，于是分析在**假数据**上进行，而断言不依赖数据的 golden
            # 照样通过（真实基线里两条 ✅ 用例的每一步都是 `SELECT 1`）。
            # 占位符必须是**空串**：sql_tool 对空 SQL 直接判 `{"ok": False, "error": "缺少 sql 参数"}`
            # → 该步响亮 FAILED，tool_success_rate 反映真相，Reflection 拿到的是真失败。
            logger.warning("步骤 %s 无法合成真实 SQL（无可用表），将按失败处理",
                           getattr(step, "id", "?"))
            return {"sql": ""}
        dim = _match_dimension_column(list(ctx.dimensions or []), cols)
        # 合成 SQL 也要尽量贴合"分析"意图：有数值列就 SUM，而不是一味 COUNT。
        # （真实 e2e 曾因退化到 COUNT 让"各区域营收"变成"各区域行数=2"。）
        num = next(
            (c for c in cols if c not in (dim,) and _is_numeric_col(c)),
            None,
        )
        # E7/02：表来自命名源（schema_search 跨源发现）→ SQL 必须路由过去
        src_disc = inp_src or _table_source(state, table) or _explicit_source(state)
        src_kw = {"source": src_disc} if src_disc else {}
        if dim and num:
            return {"sql": f'SELECT "{dim}", SUM("{num}") AS total_{num} FROM {table} '
                           f'GROUP BY "{dim}" ORDER BY total_{num} DESC LIMIT 20', **src_kw}
        if dim:
            return {"sql": f'SELECT "{dim}", COUNT(*) AS cnt FROM {table} '
                           f'GROUP BY "{dim}" ORDER BY cnt DESC LIMIT 20', **src_kw}
        return {"sql": f"SELECT * FROM {table} LIMIT 100", **src_kw}
    if tool == "freeform":
        # E2：模型在计划步骤 input.sql 里直接给出只读 SQL（守卫在 sql_tool 内）
        # E7/01：input.source 指定命名数据源（缺省 = 主源）
        sql = ((step.input or {}).get("sql") or "").strip()
        src = (getattr(step, "input", None) or {}).get("source") or _explicit_source(state)
        if sql:
            return {"sql": sql, **({"source": src} if src else {})}
        # 空 → 无效占位：交由工具判空失败（**不要**给 `SELECT 1`，见上条 sql_query 的说明）
        return {"sql": ""}
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
def _skills_payload(state: AgentState) -> dict[str, Any]:
    """本轮用户勾选的技能正文（Skills），供各阶段拼进 payload。

    **未勾选时返回空 dict**——保证"没勾选 = 行为与以前一字不差"。
    每个会产出内容的阶段都要调用：只喂 context 的话，技能里写的输出格式/口径约束
    根本到不了真正写结论和报告的地方（实测：勾了技能，报告里完全看不到效果）。
    """
    text = state.metadata.get("skills_text")
    if not text:
        return {}
    return {
        "active_skills": text,
        "active_skill_ids": state.metadata.get("skill_ids") or [],
    }


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
        # CONTEXT/01：让模型**看见**库里有哪些表，避免无歧义查询被误判为需要澄清
        "available_tables": _available_tables(state),
        # Memory 召回（§20）：短期=本会话工作上下文，长期=跨会话已验证的业务结论
        "short_term_memory": short_mem,
        "long_term_memory": long_hits,
        # P1-3：历史上写回的降级教训（如「该模型区域不可用」），供本轮规避
        "known_degradation_risks": deg_lessons,
    }
    # Skills：用户本次勾选的技能（方法论/口径/领域约束），由 context 阶段吸收进目标与假设。
    # 空则注入空 dict（保持"没勾选=行为与以前一字不差"）。
    payload.update(_skills_payload(state))
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
        ContextModel, "context", build_user_message(state.user_query, payload,
                                   budget_tokens=get_settings().prompt_budget_tokens),
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
    # E2/02：把**已发现的 schema** 交给 planner。没有它，planner 写不出 `input.sql`
    # （见 `_discovered_schema_text`）。没有 schema_search 结果时**不注入空壳键**——
    # 给了空表清单，模型反而会照着编表名。
    schema_text = _discovered_schema_text(state)
    if schema_text:
        task_context["discovered_schema"] = schema_text
    # E2/04：把「执行引擎的方言」交给 planner —— 与 `discovered_schema` **同一个模式**：
    # 判不出引擎就**不注入空壳键**（给了空内容模型反而会照着编）。
    # 先有 discovered_schema 才写得出表列名，先有 sql_dialect 才写得出**能跑**的语句。
    dialect_text = _dialect_text(state)
    if dialect_text:
        task_context["sql_dialect"] = dialect_text

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
    # Skills：技能可能约束"用哪个数据源/走哪些步骤"，必须让 Planner 看见。
    task_context.update(_skills_payload(state))
    plan, plan_err = _llm_model(
        PlanModel, "planner", build_user_message(state.user_query, task_context,
                                   budget_tokens=get_settings().prompt_budget_tokens),
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
    # E2-03：单步逻辑（依赖/预检/执行/恢复）只写在一处，见 `_run_one_step`
    results = _run_one_step(step, state)
    state.tool_results.extend(results)
    result = results[0]
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


# --------------------------------------------------------------------------- #
# E2-03：SQL 预检 —— 执行前拦方言；失败后补真实列；不逐字重跑
# --------------------------------------------------------------------------- #
_SQL_TOOLS = ("sql_query", "freeform")


def _own_sql(step: PlanStep) -> str:
    """步骤**自己写的** SQL（`input.sql`）。空串 = 没有 → 走合成路径。"""
    return str(((getattr(step, "input", None) or {}).get("sql") or "")).strip()


def _step_engine(state: AgentState, step: PlanStep) -> str:
    """该步骤的目标引擎（E7 多源：`input.source` 可指向真 PG/MySQL 库）。"""
    src = (getattr(step, "input", None) or {}).get("source")
    try:
        from ...tools.datasource import resolve_source

        _, dialect = resolve_source(src)
        return dialect or "sqlite"
    except Exception:
        return "sqlite"


def _preflight_sql_error(state: AgentState, step: PlanStep,
                         params: dict[str, Any]) -> str | None:
    """执行前的**方言**预检：非空 → 这条 SQL 不该被送去执行。

    只拦方言（构造精确、可证伪）；**schema 检查不在这里拦**——
    表/列提取有误判风险，拦掉一条正确 SQL（假红）比多跑一次贵得多。
    """
    if step.tool not in _SQL_TOOLS:
        return None
    sql = str((params or {}).get("sql") or "").strip()
    if not sql:
        return None
    engine = _step_engine(state, step)
    hints = dialect_hints(sql, engine=engine)
    if not hints:
        return None
    logger.warning("步骤 %s 的 SQL 在 %s 上跑不通（未执行）：%s",
                   getattr(step, "id", "?"), engine, "；".join(hints))
    return format_error(sql, known_schema(state), message="SQL 预检未通过（未执行）",
                        engine=engine, hints=hints)


def _enrich_sql_failure(state: AgentState, step: PlanStep, result: ToolResult) -> ToolResult:
    """失败后把**真实列清单**补进 `error` —— REPLAN 唯一能拿到的上下文。

    模型拿到的是 `no such column: f.order_id`，它得自己回忆起 `fact_sales` 的真实列；
    补上这一句，"再试一次"才有依据。
    """
    if result.status != "FAILED" or step.tool not in _SQL_TOOLS:
        return result
    sql = _own_sql(step) or str((getattr(result, "input", None) or {}).get("sql") or "")
    schema = known_schema(state)
    if not sql or not schema:
        return result
    result.error = format_error(sql, schema, message=result.error,
                                engine=_step_engine(state, step))
    return result


_NOT_PLANNABLE_MSG = (
    "`{tool}` 不能作为计划步骤：它需要 `state.analysis`，而分析阶段（Analyst）"
    "在本阶段（Executor）**之后**——此刻 analysis 还不存在。"
    "报告由 Reporter 阶段产出，不要把它排进计划。"
)


def _not_plannable_error(step: PlanStep) -> str | None:
    """步骤的工具是否**按构造做不到**（E2/05）。

    与 SQL 预检同类：**计划本身排错了一步**，所以

    * **不算 `skipped`**：`skipped` 专指"依赖未满足、根本没轮到"（E6/03），
      这里必须留在**失败分母**里，否则"排错步骤"会从指标里消失；
    * **不改道**：不"顺手帮它执行"、也不顺延到 Reporter——静默补救会把
      "planner 排了一个做不到的步骤"这件事藏起来，比响亮失败更坏。

    名单与 planner 侧**同源**（`NOT_PLANNABLE_TOOLS`），不另写一份。
    """
    try:
        from ....core.tools.specs import NOT_PLANNABLE_TOOLS
    except Exception:  # 导入故障不得拦住执行
        return None
    if step.tool not in NOT_PLANNABLE_TOOLS:
        return None
    logger.warning("步骤 %s 排了不可计划的工具 %s（未执行）",
                   getattr(step, "id", "?"), step.tool)
    return _NOT_PLANNABLE_MSG.format(tool=step.tool)


def _run_one_step(step: PlanStep, state: AgentState) -> list[ToolResult]:
    """**单步执行的全部逻辑**（依赖检查 → 预检 → 执行 → 恢复/重试）。

    `run_executor`（逐步）与 `_execute_one_step`（并发）都走这里——
    两份逐字复制的分支曾经漂移过，合成一处后不可能再漂移。
    """
    # E2/05：先拦"按构造做不到"的步骤，**排在依赖检查之前**。
    # 否则它会被记成 `依赖步骤未完成` —— D54 真实基线里 13 条假象就是这么被盖住的：
    # 那根本不是依赖问题，是这一步永远做不到。
    blocked = _not_plannable_error(step)
    if blocked is not None:
        return [ToolResult(step_id=step.id, tool=step.tool, status="FAILED",
                           error=blocked)]

    deps_ok = all(_dep_done(d, state) for d in step.dependencies)
    if not deps_ok:
        # E6/03：**未执行** ≠ 执行失败。标出来，指标才不会把它算进分母。
        return [ToolResult(step_id=step.id, tool=step.tool, status="FAILED",
                           skipped=True, error="依赖步骤未完成")]

    ensure_schema_discovered(state, [step])
    params = build_executor_params(state, step)

    preflight = _preflight_sql_error(state, step, params)
    if preflight is not None:
        # **不算 skipped**：这不是"没轮到"，而是这一步自己的 SQL 写错了——
        # 它是**真的失败**，必须留在工具成功率的分母里。
        return [ToolResult(step_id=step.id, tool=step.tool, status="FAILED",
                           error=preflight)]

    result = execute_tool(step.id, step.tool, params, state.session_id)
    results: list[ToolResult] = [result]

    # §23 确定性分支路由：Invalid Column/Table → Schema Search → Retry。
    if (result.status == "FAILED" and is_schema_error(result.error)
            and not step.id.endswith("__retry")):
        recovery = execute_tool(f"{step.id}__recovery_schema", "schema_search",
                                {"keyword": (state.context.metrics or [""])[0]},
                                state.session_id)
        results.append(recovery)
        # E2-03：**带 `input.sql` 的步骤不做逐字重跑**——`build_executor_params`
        # 会原样返回同一条 SQL，重跑必然再失败一次（真实基线里就是这样空转的）。
        # 合成路径仍要重试：那里 `_first_table` 会因新 schema 重选表。
        if recovery.status == "SUCCESS" and not _own_sql(step):
            retry_params = build_executor_params(state, step)
            results.append(execute_tool(f"{step.id}__retry", step.tool,
                                       retry_params, state.session_id))
    for r in results:
        _enrich_sql_failure(state, step, r)
    return results


def _execute_one_step(step: PlanStep, state: AgentState) -> list[ToolResult]:
    """P1-1：执行单个步骤并返回其 ToolResult 列表（**不**修改共享 state）。

    改为「纯函数式」返回，以便放进线程池并发执行：同一波次的步骤彼此依赖已满足，
    不会读写对方的 ``tool_results``，故并发安全；合并回 ``state.tool_results``
    由主线程在波次结束后统一做（避免多线程写同一 list）。

    E2-03：逻辑与 `run_executor` **共用** `_run_one_step`（此前是两份逐字复制的分支）。
    """
    return _run_one_step(step, state)


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


def _run_batch_after_discovery(steps: list[PlanStep], state: AgentState,
                               max_workers: int) -> dict[str, list[ToolResult]]:
    """先在本波**主线程**补一次 schema 发现，再并发执行。

    两处都必须这样：
    - 发现动作写在主线程——worker 线程各写 `state.tool_results` 会串（`_execute_one_step`
      刻意不修改共享 state，就是为了并发安全）；
    - 发现放在**波次级**而不是每步——同一波若有多步都需要表，补一次、全体受益。
    """
    ensure_schema_discovered(state, steps)
    return _run_batch(steps, state, max_workers)


def _executor_all_steps(state: AgentState) -> Iterator[AgentState]:
    """P1-1 波次并发执行的**逐步快照内核**（run_executor_all / 流式共用）。

    执行语义与原 ``run_executor_all`` 完全一致（波次划分 / 失败占位 /
    save_last_dataset 时机均不变），唯一区别是**每完成一个计划步骤就 yield
    一次快照**（status 保持 EXECUTE），最后推进到 ANALYZE 并 yield 终态快照。
    同步路径把快照逐个丢弃即可，两条路径不会漂移。
    """
    state.status = "EXECUTE"
    steps = state.plan.steps
    if not steps:
        state.status = "ANALYZE"
        yield state
        return
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
                                   skipped=True, error="依赖步骤未完成"))
                    done_ids.add(s.id)
            break
        batch = _run_batch_after_discovery(ready, state, max_workers)
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
            yield state  # 每步一次快照：SSE 路径的 EXECUTE 帧来源
    state.current_step_index = len(steps)
    state.status = "ANALYZE"
    yield state


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

    实现为消费 :func:`_executor_all_steps` 内核（忽略中间快照），保证与流式
    路径 (:func:`iter_executor_all`) 的执行语义同源、不漂移。
    """
    for _ in _executor_all_steps(state):
        pass
    return state


def iter_executor_all(state: AgentState) -> Iterator[AgentState]:
    """``run_executor_all`` 的流式版本：每完成一个工具步骤即 yield 一次快照。

    为什么必须存在：SSE 路径此前只在 ``run_executor_all`` **返回后**拿到单个
    快照，而彼时 status 已推进为 ANALYZE —— 前端永远收不到 EXECUTE 帧，
    「执行完成 · 0 个工具」就是这条 bug（工具明明执行了，UI 计数恒为 0，
    时间线里也没有任何工具步骤卡片）。

    span 语义与 ``@trace("executor")`` 一致：在 ``trace_run`` 上下文内手动开
    / 关 executor span，保证 eval 的 tool_calls（数 executor span）在流式
    路径同样计数，不会因为换入口而失真。
    """
    from ....infrastructure.observability.tracing import _current_tracer

    tracer = _current_tracer.get()
    span = tracer.start("executor") if tracer is not None else None
    ended = False

    def _end(ok: bool, error: BaseException | None = None) -> None:
        nonlocal ended
        if tracer is not None and span is not None and not ended:
            ended = True
            tracer.end(span, ok=ok, error=error)

    try:
        yield from _executor_all_steps(state)
        _end(True)
    except BaseException as exc:  # noqa: BLE001 — 生成器需同时兜 GeneratorExit
        _end(False, exc)
        raise


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
    # `ok=_analysis_usable`：**「能过校验」≠「可用」**——AnalysisResult 字段全有默认值，
    # 工具调用 JSON / 别的阶段的 schema / `{role,content}` 壳都能"校验通过"并得到全空分析。
    # 真实基线 7 条用例 `findings=0`、E1 溯源 `0/0` 就是这么来的（无声）。
    # Skills：口径/方法论约束（如"金额一律用不含税口径"）直接影响结论，必须让 Analyst 看见。
    payload.update(_skills_payload(state))
    state.analysis, ana_err = _llm_model(
        AnalysisResult, "analyst", build_user_message(state.user_query, payload,
                                   budget_tokens=get_settings().prompt_budget_tokens),
        ok=_analysis_usable,
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
        ReflectionResult, "reflection", build_user_message(state.user_query, payload,
                                   budget_tokens=get_settings().prompt_budget_tokens),
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


_REPORT_FIELD_NAMES = ("report", "markdown", "content", "text")


def _sanitize_report_output(raw: Any, template: str) -> tuple[str, str | None]:
    """(报告文本, 兜底原因)。**"像报告的文本"才配当报告。**

    报告必须是 Markdown 文本，但真实模型会返回别的东西。真跑实测（2026-09-13，
    `nvidia/nemotron-3-super-120b-a12b`）：模型返回工具调用 JSON
    ``{"tool": "schema", "args": {}}``，被原样当成报告发给用户——报告 36 字符、
    0 条 findings。原实现的唯一守卫是 MockLLM 的 ``__markdown__`` 信号，
    即"只防自己人"。

    三类处理：
    - MockLLM 信号 → 走模板（历史行为）；
    - Markdown 被**包在 JSON 字段里**（模型常见）→ 取出内层，不丢内容；
    - 其它 JSON 对象（含工具调用）→ 走模板，并**记下原因**（铁律 3：不静默降级）。
    """
    if not raw or not isinstance(raw, str):
        return template, "reporter 输出为空"
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return raw, None          # 正常 Markdown
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return raw, None          # 以 { 开头但不是合法 JSON → 当作 Markdown 照用
    if not isinstance(parsed, dict):
        return raw, None
    if "__markdown__" in parsed:
        return template, "MockLLM 的 __markdown__ 信号"
    for key in _REPORT_FIELD_NAMES:
        inner = parsed.get(key)
        if isinstance(inner, str) and inner.strip():
            return inner, f"Markdown 被包在 JSON 字段 {key!r} 里"
    return template, f"输出是 JSON 对象而非报告（keys={sorted(parsed)[:5]}）"


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
            # Skills：报告是用户唯一直接看到的东西，技能里的格式/口径/披露要求
            # 必须传到这一层，否则"勾了技能"对最终交付物零影响。
            **_skills_payload(state),
        }, ensure_ascii=False, default=str), json_mode=False)
        # REP/02：模型输出**净化**——报告必须是 Markdown。
        # 真跑实测：模型返回工具调用 JSON 时曾被原样当报告发出（见 _sanitize_report_output）。
        report_text, fallback_reason = _sanitize_report_output(raw, tpl.get("report", ""))
        state.report = report_text
        if fallback_reason:
            # 铁律 3：兜底属降级，必须可观测，不许静默。
            state.metadata["reporter_fallback"] = fallback_reason
            logger.warning("reporter 输出不可用，已回退模板报告：%s", fallback_reason)
    # E1：报告追加「数字来源」块（每条数值 evidence → [src: step_id]）
    try:
        from ....core.agents.data_analyst.sources import append_citations
        state.report = append_citations(state.report or "", state.analysis, state.tool_results)
    except Exception:
        pass
    # D51：报告图内嵌 —— 有图才追加 `## 图表`（无图不留空标题）；
    # 图源只认落在本会话工作目录里、真实存在的成功产物（见 charts.collect_charts）
    try:
        from ....core.agents.data_analyst.charts import collect_charts, embed_charts

        state.report = embed_charts(state.report or "", collect_charts(state),
                                    state.session_id)
    except Exception:
        pass  # 嵌图故障不得打断报告
    # D41：把本轮的溯源覆盖 / 疑似幻觉计入 /metrics（与 eval **同一口径**）
    try:
        from ....core.agents.data_analyst.sources import trace_coverage
        from ....infrastructure.observability.metrics import record_trace_coverage

        cov = trace_coverage(state.analysis.findings, state.tool_results)
        record_trace_coverage(cov["numeric_claims"], cov["traced_claims"])
    except Exception:
        pass  # 埋点故障不得打断报告
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
