from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

AgentStatus = Literal[
    "INIT",
    "UNDERSTAND",
    "DISCOVER",
    "PLAN",
    "PREPARE",
    "EXECUTE",
    "VALIDATE",
    "ANALYZE",
    "REFLECT",
    "REPLAN",
    "REPORT",
    "FINISH",
    "CLARIFY",   # CLARIFY/01：一次对话回合的终止态（等用户回答）
    "ERROR",
    "FAILED",
]


class ModelOutputError(RuntimeError):
    """模型结构化输出的**无法修复**的校验失败。

    与"工具报错"不同：这是 **LLM 没按 schema 输出**，且重试后仍然不合法。
    调用方（编排层）应把它转成 ``status=ERROR``，而不是让 ``ValidationError``
    裸穿透 —— 实测一次真实 eval 重跑就因为 planner 的畸形输出
    丢掉了全部 7 条基线（详见 docs/progress/pending-real.md §C.3）。
    """


class ReflectionDecision(str, Enum):
    PASS = "PASS"
    REPLAN = "REPLAN"
    FAIL = "FAIL"


class ToolResult(BaseModel):
    """Structured result returned by an executed tool (Executor contract)."""

    step_id: str
    tool: str
    status: Literal["SUCCESS", "FAILED", "PARTIAL"] = "SUCCESS"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    execution_time_ms: int = 0
    error: Optional[str] = None
    # 错误处理协议（§23）：RETRYABLE / NON_RETRYABLE，供 Reflection 路由 REPLAN vs FAIL
    error_class: Optional[str] = None
    attempts: int = 1
    artifacts: list[str] = Field(default_factory=list)


class TimeRange(BaseModel):
    """Spec §10 – structured time range (LLM 输出可能是字符串/None，统一容错)."""

    start: Optional[str] = None
    end: Optional[str] = None
    timezone: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        return data if isinstance(data, dict) else {}


class Comparison(BaseModel):
    """Spec §10 – comparison baseline."""

    type: Optional[str] = None
    period: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        return data if isinstance(data, dict) else {}


class ContextModel(BaseModel):
    objective: str = ""
    analysis_object: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    time_range: TimeRange = Field(default_factory=TimeRange)
    comparison: Comparison = Field(default_factory=Comparison)
    output_format: str = "report"
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    raw: Optional[dict[str, Any]] = None


class PlanStep(BaseModel):
    id: str
    objective: str
    action: str
    tool: str
    dependencies: list[str] = Field(default_factory=list)
    expected_output: str = ""
    success_criteria: str = ""
    # E2：模型可直接在步骤里给出自定义 SQL/代码（tool=freeform 时消费）
    input: Optional[dict[str, Any]] = None


class PlanModel(BaseModel):
    goal: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    stopping_criteria: list[str] = Field(default_factory=list)
    risk_points: list[str] = Field(default_factory=list)
    raw: Optional[dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """真实 LLM 常把字符串列表写成 [[\"a\"]]、{\"a\": 1} 或单个字符串。

        Mock/严格 schema 永远不会暴露这种偏差；一旦换真实模型，
        `stopping_criteria: [["Core business question answered"]]` 会直接
        ValidationError 把整个 planner 阶段打挂（整跑失败）。
        这里统一展平：字符串 → 单元素；dict → 取 keys 或 values；嵌套 list → 展平。
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for key in ("stopping_criteria", "risk_points"):
            if key in out:
                out[key] = _coerce_str_list(out[key])
        steps = out.get("steps")
        if isinstance(steps, list):
            coerced = [_coerce_step(s) for s in steps]
            # 只剩 id、既无 objective 也无 action 的**退化条目**：丢弃而不是硬塞默认值。
            # 硬塞会让它变成一次"无意义的 sql_query 工具调用"，比丢掉更糟；
            # 丢了多少记进 raw._dropped_steps，可审计（不是静默）。
            kept, dropped = [], 0
            for s in coerced:
                if isinstance(s, dict) and not (s.get("objective") or s.get("action")):
                    dropped += 1
                    continue
                kept.append(s)
            out["steps"] = kept
            if dropped:
                raw = dict(out.get("raw") or {}) if isinstance(out.get("raw"), dict) else {}
                raw["_dropped_steps"] = dropped
                out["raw"] = raw
        return out


def _coerce_str_list(value: Any) -> list[str]:
    """把任意松散结构展平成 list[str]。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        # {"criterion": "desc"} → 优先取值，其次取键
        vals = [v for v in value.values() if isinstance(v, (str, int, float))]
        return [str(v) for v in vals] or [str(k) for k in value]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_coerce_str_list(item))
        return out
    return [str(value)]


def _coerce_step(step: Any) -> Any:
    """步骤条目容错：允许字符串（转成最小步骤）或 dict（补默认字段）。"""
    if isinstance(step, str):
        return {"id": step, "objective": step, "action": step, "tool": "sql_query"}
    if not isinstance(step, dict):
        return step
    out = dict(step)
    # 工具名容错：模型可能写成 "SQL Query" / "sql-query"
    if isinstance(out.get("tool"), str):
        out["tool"] = out["tool"].strip().lower().replace(" ", "_").replace("-", "_")
    for key in ("dependencies",):
        if key in out:
            out[key] = [str(x) for x in _coerce_str_list(out[key])]
    for key in ("objective", "action", "expected_output", "success_criteria"):
        if isinstance(out.get(key), list):
            out[key] = "；".join(_coerce_str_list(out[key]))
    if not out.get("id"):
        out["id"] = f"step_{abs(hash(str(out.get('objective', '')))) % 10**6}"
    # 必填字段补默认（PlanStep 的 objective/action/tool 是必填）。
    # 真实（尤其免费/小）模型会产出 `{"id": "step_0"}` 这种**退化条目**；
    # 此前只补 id 不补这三个 → ValidationError 打挂整个 planner（整跑失败）。
    if not out.get("objective"):
        out["objective"] = out.get("action") or out.get("expected_output") or ""
    if not out.get("action"):
        out["action"] = out["objective"] or ""
    if not isinstance(out.get("tool"), str) or not out["tool"].strip():
        out["tool"] = "sql_query"
    return out


class Evidence(BaseModel):
    """Spec §10 – a single piece of evidence behind a finding."""

    source: str = ""
    metric: Optional[str] = None
    value: Any = None
    comparison: Any = None
    impact: Optional[str] = None
    # E1 数字溯源：数值型 evidence 应链到一条真实、成功的 sql_query step
    sql_id: Optional[str] = None
    sql_text_hash: Optional[str] = None
    row_sample: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        # 真实 LLM 可能把证据写成纯文本；包装成可追溯结构
        if isinstance(data, str):
            return {"source": "analyst", "value": data}
        if data is None:
            return {}
        return data


class Finding(BaseModel):
    finding: str
    evidence: list[Evidence] = Field(default_factory=list)
    interpretation: str = ""
    confidence: float = 0.0


_HYPOTHESIS_RESULTS = ("SUPPORTED", "PARTIALLY_SUPPORTED", "REJECTED", "UNKNOWN")


class Hypothesis(BaseModel):
    """Spec §10 – a tested hypothesis with its verdict."""

    hypothesis: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    result: Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "REJECTED", "UNKNOWN"] = "UNKNOWN"
    confidence: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"hypothesis": data}
        if isinstance(data, dict):
            d = dict(data)
            if not d.get("hypothesis"):
                d["hypothesis"] = d.get("text") or d.get("description") or d.get("statement") or ""
            r = d.get("result")
            if isinstance(r, str):
                r2 = r.strip().upper().replace(" ", "_").replace("-", "_")
                d["result"] = r2 if r2 in _HYPOTHESIS_RESULTS else "UNKNOWN"
            else:
                d["result"] = "UNKNOWN"
            if isinstance(d.get("evidence"), str):
                d["evidence"] = [d["evidence"]]
            return d
        return data


class Recommendation(BaseModel):
    """Spec §10 – an evidence-linked action item."""

    problem: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    action: str = ""
    expected_impact: Optional[str] = None
    priority: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"action": data}
        if isinstance(data, dict):
            d = dict(data)
            if not d.get("action"):
                d["action"] = d.get("text") or d.get("recommendation") or ""
            p = d.get("priority")
            if isinstance(p, str):
                p2 = p.strip().upper()
                d["priority"] = p2 if p2 in ("HIGH", "MEDIUM", "LOW") else "MEDIUM"
            if isinstance(d.get("evidence"), str):
                d["evidence"] = [d["evidence"]]
            return d
        return data


class AnalysisResult(BaseModel):
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    # E4/04 质量门禁：由确定性检查写入的披露文本（不进 LLM，也不改数值）
    quality_notes: list[str] = Field(default_factory=list)
    # E5/01 统计声明（LLM 产出；门禁据此判断"该声明的有没有声明"）
    stats_notes: list[StatsNote] = Field(default_factory=list)
    raw: Optional[dict[str, Any]] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_lists(cls, data: Any) -> Any:
        """Real LLMs don't always honour the schema strictly.

        - ``limitations`` is declared as ``list[str]`` but a live model may return
          a list of structured dicts (``{"category": ..., "description": ...}``).
          Coerce each item to a readable string.
        - ``metrics`` / ``hypotheses`` / ``recommendations`` are typed models;
          a live model may return plain strings. Wrap them so the field-level
          validators can coerce them into the spec §10 shapes.
        """
        if not isinstance(data, dict):
            return data
        if isinstance(data.get("limitations"), list):
            coerced = []
            for item in data["limitations"]:
                if isinstance(item, dict):
                    text = (
                        item.get("category")
                        or item.get("description")
                        or item.get("detail")
                        or item.get("text")
                        or json.dumps(item, ensure_ascii=False)
                    )
                    coerced.append(str(text))
                else:
                    coerced.append(str(item))
            data["limitations"] = coerced
        for fld in ("metrics", "hypotheses", "recommendations"):
            if isinstance(data.get(fld), list):
                data[fld] = [
                    # BaseModel 实例必须原样放行：此前只认 dict，
                    # 传 `Hypothesis(...)` 会被包成 {"text": repr} —— result 等字段**静默丢失**。
                    item if isinstance(item, (dict, BaseModel)) else {"text": str(item)}
                    for item in data[fld]
                ]
        return data


class StatsNote(BaseModel):
    """E5/01 统计严谨：一条结论的统计声明（方法/样本量/显著性/口径局限）。

    LLM 产出，门禁只检查"该声明的有没有声明"，不判断"算得对不对"
    （正确性依赖真实模型，标 [待真实验证]）。
    """

    claim: str = ""
    method: str = ""
    n: Optional[int] = None
    significant: Optional[bool] = None
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"claim": data}
        if isinstance(data, dict):
            d = dict(data)
            if not d.get("claim"):
                d["claim"] = d.get("text") or d.get("metric") or ""
            n = d.get("n")
            if isinstance(n, str):
                d["n"] = int(n) if n.strip().isdigit() else None
            sig = d.get("significant")
            if isinstance(sig, str):
                d["significant"] = sig.strip().lower() in ("true", "yes", "是", "显著")
            return d
        return {}


class AdversarialCheck(BaseModel):
    """E5/02 对抗性指令检查结果（结构化，供 API/eval 断言）。"""

    dq_override_requested: bool = False
    refused: bool = False
    unmarked_quality_issues: list[str] = Field(default_factory=list)
    untested_comparisons: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        return data if isinstance(data, dict) else {}


class CaliberIssue(BaseModel):
    """E4/03 口径问题（结构化，供 eval 断言——不塞进 ReflectionDimension 的字符串列表）。"""

    kind: Literal["period_mismatch", "filter_mismatch", "denominator_missing",
                  "iteration_drift", "unit_mismatch"] = "period_mismatch"
    detail: str = ""
    metric: Optional[str] = None


class CaliberCheck(BaseModel):
    """口径可比性检查结果（确定性规则优先，LLM 的语义判断为补充）。"""

    comparable: bool = True
    checked_metrics: list[str] = Field(default_factory=list)
    issues: list[CaliberIssue] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        """真实 LLM 可能把 issues 写成字符串列表或整段文本。"""
        if not isinstance(data, dict):
            return {}
        d = dict(data)
        issues = d.get("issues")
        if isinstance(issues, str):
            d["issues"] = [{"detail": issues}]
        elif isinstance(issues, list):
            d["issues"] = [{"detail": i} if isinstance(i, str) else i for i in issues]
        return d


class ReflectionDimension(BaseModel):
    """Spec §10 – one quality-gate dimension (score + issues)."""

    score: float = 0.0
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, (int, float)):
            return {"score": cls._normalize_score(data)}
        if isinstance(data, dict):
            d = dict(data)
            s = d.get("score")
            if isinstance(s, (int, float)):
                d["score"] = cls._normalize_score(s)
            elif isinstance(s, str):
                try:
                    d["score"] = cls._normalize_score(float(s))
                except ValueError:
                    d.pop("score", None)
            iss = d.get("issues")
            if isinstance(iss, str):
                d["issues"] = [iss]
            elif isinstance(iss, list):
                d["issues"] = [
                    i if isinstance(i, str) else json.dumps(i, ensure_ascii=False)
                    for i in iss
                ]
            return d
        return {}

    @staticmethod
    def _normalize_score(s: float) -> float:
        # 真实 LLM 可能给 0-100 制分数（如 85）；统一为 0-1
        return s / 100.0 if 1.0 < s <= 100.0 else max(0.0, min(1.0, s))


class ReflectionResult(BaseModel):
    decision: ReflectionDecision = ReflectionDecision.REPLAN
    confidence: float = 0.0
    data_quality: ReflectionDimension = Field(default_factory=ReflectionDimension)
    metric_quality: ReflectionDimension = Field(default_factory=ReflectionDimension)
    evidence_coverage: ReflectionDimension = Field(default_factory=ReflectionDimension)
    logical_validity: ReflectionDimension = Field(default_factory=ReflectionDimension)
    completeness: ReflectionDimension = Field(default_factory=ReflectionDimension)
    business_relevance: ReflectionDimension = Field(default_factory=ReflectionDimension)
    missing_evidence: list[str] = Field(default_factory=list)
    replan_objectives: list[str] = Field(default_factory=list)
    summary: str = ""
    # E4/03 口径可比性（确定性检查写入；LLM 的判读为补充）
    caliber_comparability: CaliberCheck = Field(default_factory=CaliberCheck)
    # E5/02 对抗性指令（确定性检查写入）
    adversarial: AdversarialCheck = Field(default_factory=AdversarialCheck)
    raw: Optional[dict[str, Any]] = None


class AgentState(BaseModel):
    """The single state object threaded through the LangGraph pipeline.

    Every node reads from and writes to this model. It is intentionally a
    ``dict``-friendly Pydantic model so LangGraph (which passes plain dicts)
    can serialize it, while we keep typed accessors for the nodes.
    """

    status: AgentStatus = "INIT"
    session_id: str = ""
    user_query: str = ""
    mode: str = "full"  # ROUTE: full|sql_only|quick_answer|markdown_doc|python_code
    intent: Optional[dict[str, Any]] = None  # ROUTE/02 ExecutionPlan (task_type/deliverable/workflow)
    last_dataset: Optional[dict[str, Any]] = None  # E3 会话数据集（增量迭代用）
    attachment_context: str = ""  # 用户上传附件（由 AttachmentStore 渲染的上下文文本）
    iteration: Optional[dict[str, Any]] = None  # E3/02 增量本轮信息（kind/stages/skips/attempts）
    force_full_rerun: bool = False  # E3/02 显式全链开关（压过增量）
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    context: ContextModel = Field(default_factory=ContextModel)
    plan: PlanModel = Field(default_factory=PlanModel)
    current_step_index: int = 0
    tool_results: list[ToolResult] = Field(default_factory=list)
    analysis: AnalysisResult = Field(default_factory=AnalysisResult)
    reflection: Optional[ReflectionResult] = None
    report: str = ""
    replan_count: int = 0
    max_replans: int = 2
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}
