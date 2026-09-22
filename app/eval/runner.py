"""Run the golden set through the pipeline and aggregate quality/cost metrics.

Deterministic per-case data (tool calls / approx LLM calls) is read back from
the persisted structured trace (``trace_run`` writes ``data/traces/{session}.jsonl``),
so numbers are real, not fabricated.

Usage::

    python -m app.eval.runner --mode mock
    python -m app.eval.runner --mode real   # needs LLM_API_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.infrastructure.llm.router import reset_llm
from app.infrastructure.observability import tracing as tracing_mod
from app.infrastructure.observability.tracing import load_run

from .golden import GOLDEN, GoldenCase

# 真正驱动 LLM 的编排节点（executor 是确定性工具调用，不计入 LLM 次数）
_LLM_STAGES = {"context", "planner", "analyst", "reflection", "reporter"}


def _set_mode(mode: str) -> None:
    os.environ["MOCK_LLM"] = "true" if mode == "mock" else "false"
    get_settings.cache_clear()
    reset_llm()


@dataclass
class CaseOutcome:
    case_id: str
    status: str
    error: str | None = None
    tool_calls: int = 0
    llm_calls: int = 0
    findings: int = 0
    reflect_decision: str | None = None
    report_len: int = 0
    report_text: str = ""
    assertions_ok: bool = False
    failed_assertions: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    executed_tools: list[str] = field(default_factory=list)
    tool_success: int = 0
    tool_fail: int = 0
    # E6/03：因**上游未完成**而根本没进执行器的调用数（不算失败，也不算成功）
    tool_skipped: int = 0
    numeric_claims: int = 0
    traced_claims: int = 0
    # E6/01：结构化断言结果（读 state，不靠字符串匹配）
    quality_codes: list[str] = field(default_factory=list)
    caliber_kinds: list[str] = field(default_factory=list)
    refused: bool = False
    skipped_reason: str | None = None
    # LLM-judge（#5）：评判「答案对不对」的语义分（0~1）。离线 rubric 或真 LLM。
    judge_score: float | None = None
    judge_method: str | None = None
    judge_rationale: str = ""
    # **测量诚信**：本轮是否发生过 LLM 降级（限流/余额/上游故障 → 退化成 mock）。
    # real 模式下这种用例的分数**不是真实模型的表现**，必须剔除而不是计分。
    degraded: bool = False
    degraded_stages: list[str] = field(default_factory=list)
    # E6/02：报告正文里**在全部证据中都找不到出处**的大额数值（疑似编造）。
    ungrounded_numbers: list[str] = field(default_factory=list)
    # E7：单用例成本（prompt_tokens + completion_tokens × 配置单价）；
    # None = 压根没配单价或本轮无 token 消耗。每个用例独立可见，便于真实基线定位"谁烧的钱"。
    cost_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "report_text"}
        return d


def compute_cost_usd(cost_in: float | None, cost_out: float | None,
                     prompt_tokens: int, completion_tokens: int) -> float | None:
    """token 数 + 单价 → USD；**单价未知（None）→ 返回 None**（纯函数，便于离线测试）。

    语义（与 `config.py` 的 `cost_*_per_mtok` 对齐）：
    - 单价 **None**（压根没配）→ `None`，表示"不知道花了多少钱"；
    - 单价 **显式 0**（免费档，如 `-free` 模型）→ `0.0`，表示"确实不花钱"，**要报出来**。

    这两件事以前用同一个 `0.0` 兼表，于是成本列**恒为 None**：
    免费模型的真实成本 0 与"没配单价"在报告里长得一模一样。
    """
    if cost_in is None and cost_out is None:
        return None
    if not (prompt_tokens or completion_tokens):
        return None
    return round(prompt_tokens / 1e6 * (cost_in or 0.0)
                 + completion_tokens / 1e6 * (cost_out or 0.0), 6)


def hallucination_rate(numeric_claims: int, traced_claims: int) -> float | None:
    """**疑似幻觉率** = 1 − 溯源覆盖率。

    定义沿用 E1：数值结论若拿不到对应 SQL 步骤，即计为疑似编造的数字。

    **零 claim → `None`（未定义），不是 0.0。**
    把"没测到"报成"0 幻觉"是最典型的自欺——D38 的 `溯源 0/0` 就是这样全程判过的
    （也正因如此才有 `min_numeric_claims` 去要求"至少有一条数值结论"）。
    """
    if not numeric_claims:
        return None
    return round(1 - traced_claims / numeric_claims, 4)


def numeric_claim_violations(case: GoldenCase, claims: int) -> list[str]:
    """`min_numeric_claims` 判定（纯函数）。

    **补的是 E1 的一个 vacuous 漏洞**：现有断言是"每个数值 claim 都要可溯源"，
    当数值 claim **一个都没有**时它**恒真**——D38 真实基线 `溯源 0/0` 全程判过，
    于是"分析停留在元讨论、没给出任何数据结论"这件事**测不出来**。
    """
    if claims < case.min_numeric_claims:
        return [f"本轮仅 {claims} 条可溯源数值结论，少于要求的 {case.min_numeric_claims} 条"
                "（分析未产出数据结论——E1 的「每个都要可溯源」在零 claim 时恒真）"]
    return []


def _number_tokens(text: str) -> list[str]:
    """文本里的数值字面量（保序、含千分位）。"""
    return re.findall(r"\d[\d,]*\.?\d*", text or "")


def _as_float(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _ground_numbers(tool_results, findings) -> list[float]:
    """「出处」数值集合：**成功工具结果的输出** + **经核对的** finding evidence 取值。

    只认**成功**的工具结果。失败结果的 output 不是证据——否则一条报错的查询
    会成为编造数字的挡箭牌。

    **`evidence.value` 不是无条件算数**（D54 用真实产物复现出来的洞）：
    在 `data/checkpoints/eval_q_region_top_0b7e93.json` 上，报告里那张编造的
    区域营收表（华东 1,245,000 / 华南 890,000 / 西部 320,000…）**同时**出现在
    `findings[].evidence[].value` 里，且每条都写着 `sql_id: "step_5"`、
    `row_sample: "[]"`——而 `step_5` 实际执行的是一条 3 行维表的查询，
    **输出里根本没有这些数字**。

    无条件采信 `evidence.value` 就等于**让模型的自我声明给自己作证**：
    编一个数、把它写进 evidence、报告里再写一遍 → 全部"有出处"。
    所以加一道核对：evidence 的 `value` 只有在**它声称的那条 SQL 步骤的输出里
    真能找到同一个数**时才算出处。

    - `sql_id` 缺失 / 悬空 / 对应步骤不是 SUCCESS → **不算出处**
      （与 `lineage.py` 的"无 `sql_id` → `traced=False`，绝不借来源"同一纪律）；
    - 数值是否"找得到"用同一套 `rel_tol` 容差（四舍五入/单位换算后的呈现）。
    """
    out: list[float] = []
    by_step: dict[str, list[float]] = {}
    for r in (tool_results or []):
        if getattr(r, "status", "") != "SUCCESS":
            continue
        blob = json.dumps(getattr(r, "output", None) or {}, ensure_ascii=False, default=str)
        nums = [v for v in (_as_float(t) for t in _number_tokens(blob)) if v is not None]
        out.extend(nums)
        sid = str(getattr(r, "step_id", "") or "")
        if sid:
            by_step.setdefault(sid, []).extend(nums)
    for f in (findings or []):
        for ev in (getattr(f, "evidence", None) or []):
            v = _as_float(str(getattr(ev, "value", "")))
            if v is None:
                continue
            sid = str(getattr(ev, "sql_id", "") or "")
            if not sid or sid not in by_step:
                continue
            if any(abs(t - v) <= max(1e-6, 0.01 * abs(v)) for t in by_step[sid]):
                out.append(v)
    return out


def _is_year_like(token: str) -> bool:
    """四位整数且落在 1900–2100：报告里的**年份**，不是业务量。

    为什么必须显式排除（而不是指望 `min_abs`）：`2024 >= 1000`，
    阈值拦不住它，而"2024 年 Q3"这种写法**每份按时段分析的报告都有**——
    一次系统性假红会让门禁被直接关掉（狼来了）。代价是"年份区间内的编造值"
    会漏，这是**有意的、有界的**取舍：`min_abs` 已经声明本门禁不追小额数字。
    """
    if not re.fullmatch(r"\d{4}", token):
        return False
    return 1900 <= int(token) <= 2100


def ungrounded_numbers(report_text, tool_results, findings, *,
                       min_abs: float = 1000.0, rel_tol: float = 0.01,
                       limit: int = 20) -> list[str]:
    """报告正文里**在全部证据中都找不到出处**的大额数值（去重、保序、限量）。

    补的是 E1 的一处结构性缺口：`unresolved_numeric_claims` 只遍历
    **`findings[].evidence[].value`**，**报告正文的数值从来不检查**。
    于是真实基线里 `q_region_top` 能在只跑了一条 `SELECT * FROM dim_channel`
    的情况下，**凭空写出一整张区域营收表**（华东 1,245,000 / +18.5% / 占比 32%）
    —— findings 是干净的，正文是编的 —— **却判 ✅**。

    参数选择的两条理由：

    - `min_abs=1000`：报告天然有大量结构性小数（`+18.5%`、`TOP 10`、`3 个渠道`）。
      全都要溯源会把**正确**的报告判红（假红）；
      **大额数字编不出来才是幻觉的特征**。
    - **年份另行排除**（`_is_year_like`）：`2024 >= 1000`，阈值拦不住 `2024-09`、
      `2024 年 Q3`，而按时段分析的报告**每份都有**——见该函数的说明。
    - `rel_tol=0.01`：报告写 `1,244,893`、SQL 返回 `1245000.0` 是同一件事的
      两种呈现（四舍五入/单位换算）。绝对容差会让大额数字必然假红。
    """
    if not report_text:
        return []
    ground = _ground_numbers(tool_results, findings)
    seen: set[str] = set()
    bad: list[str] = []
    for tok in _number_tokens(str(report_text)):
        v = _as_float(tok)
        if v is None or abs(v) < min_abs or _is_year_like(tok):
            continue
        if any(abs(g - v) <= max(1e-6, rel_tol * abs(v)) for g in ground):
            continue
        if tok in seen:
            continue
        seen.add(tok)
        bad.append(tok)
        if len(bad) >= limit:
            break
    return bad


def grounding_violations(case: GoldenCase, bad: list[str]) -> list[str]:
    """`max_ungrounded_numbers` 判定（纯函数，便于离线测试）。

    `-1`（默认）= 本用例不检查——mock 模式报告由模板渲染，
    无条件打开会把"流水线跑通"的既有基线判红。只在**确实可能编数字**的用例上开。
    """
    if case.max_ungrounded_numbers < 0 or len(bad) <= case.max_ungrounded_numbers:
        return []
    return [f"报告正文有 {len(bad)} 个大额数值在全部工具结果/证据中都找不到出处"
            f"（允许 {case.max_ungrounded_numbers} 条）："
            f"{'、'.join(bad[:5])}{' …' if len(bad) > 5 else ''}"
            "——数值必须来自真实查询，不得凭空给出"]


def findings_violations(case: GoldenCase, findings_count: int) -> list[str]:
    """`min_findings` 判定（纯函数，便于离线测试）。

    为什么需要它：`must_find` 是**子串命中**，而报告天然**回显问题**（标题/目标段）。
    于是"问题里出现过的词"会让断言恒真——哪怕本轮 0 条 findings、正文写"无法完成"。
    真实基线里 `r_join_amplification_guard` 正是这么被误判成 ✅ 的。

    注意与 `accept_clarify` 的关系：反问（`CLARIFY_OK`）在本函数之前就早退了，
    所以两者不冲突——**反问可不产出发现；但一旦选择作答，就必须真有发现**。
    """
    if findings_count < case.min_findings:
        return [f"本轮仅产出 {findings_count} 条发现，少于要求的 {case.min_findings} 条"
                f"（报告回显了问题、但分析并未产出结论）"]
    return []


def compute_gates(metrics: dict[str, Any], ungrounded_total: int, *,
                  mode: str = "real") -> dict[str, Any]:
    """运行级门禁（纯函数，便于离线测试与报告渲染）。

    E6/02：把"幻觉率"从**一行指标**升级成**能否决的判据**。真实基线里
    `疑似幻觉率 0.667` 对 ✅/❌ 毫无影响，`q_region_top` 编了一整张表照样通过。

    | 门禁 | 失败条件 | 理由 |
    |---|---|---|
    | `hallucination` | `hallucination_rate > 0` | 有数值结论就必须有出处。**`None`（零 claim）视为"未定义"→ 不否决**：那是"没测到"，不是"有幻觉" |
    | `grounded_numbers` | 全部用例的无源大额数值合计 > 0 | 唯一抓得住"编表"的一条 |
    | `evidence` | `degraded_excluded > 0`，或 **`real` 模式下** `skipped_requires_real > 0` | 铁律 6：跳过/降级不得混进"通过" |

    **为什么 `skipped` 只在 `real` 模式否决**：`requires_real` 用例在 mock 下
    **按设计跳过**——那是这个模式的定义，不是缺陷。若不计模式地否决，
    `--mode mock --strict` 将**永远**退出 2，门禁立刻沦为噪音并被绕过。
    `degraded_excluded` 则两种模式都否决：它代表"本该是真实模型产出、
    实际是 Mock 模板"，是真缺陷。
    """
    rate = metrics.get("hallucination_rate")
    hall = {
        "value": rate,
        "threshold": 0.0,
        "passed": rate is None or rate <= 0.0,
    }
    ground = {"violations": int(ungrounded_total), "passed": int(ungrounded_total) <= 0}
    skipped = int(metrics.get("skipped_requires_real") or 0)
    degraded = int(metrics.get("degraded_excluded") or 0)
    evidence = {"skipped": skipped, "degraded": degraded,
                "skipped_counts": mode == "real",
                "passed": degraded == 0 and not (mode == "real" and skipped > 0)}
    return {
        "mode": mode,
        "hallucination": hall,
        "grounded_numbers": ground,
        "evidence": evidence,
        "passed": hall["passed"] and ground["passed"] and evidence["passed"],
    }


def resolve_terminal(case: GoldenCase, status: str, error: str | None) -> tuple[str, list[str]]:
    """终态归一 → ``(有效状态, 失败断言)``（纯函数，便于离线测试）。

    - ``FINISH`` → 原样通过；
    - ``CLARIFY`` 且 `case.accept_clarify` → 归一为 **``CLARIFY_OK``**（可接受终态，不算失败）；
    - 其它 → 沿用 ``expect_finish`` 判定。

    ``CLARIFY_OK`` 与 ``FINISH`` **刻意区分**：判断型问题反问是对的，但它是"没做分析"，
    不能混进 FINISH 率——metrics 里单独计数 ``clarify_accepted``。
    """
    if status == "CLARIFY" and case.accept_clarify:
        return "CLARIFY_OK", []
    failures: list[str] = []
    if case.expect_finish and status != "FINISH":
        failures.append(f"期望 FINISH，实际 {status}: {error}")
    return status, failures


def quality_code_violations(case: GoldenCase, codes: list[str]) -> list[str]:
    """质量门禁 code 的正/负向断言 → 失败信息清单（纯函数，便于离线测试）。

    - ``expect_quality_codes``：必须出现，缺失即失败；
    - ``must_not_have_quality_codes``：必须**不**出现，出现即失败。

    负向断言的存在理由：有些 code 只在 Agent **写错**时才产生
    （如 ``join_amplified_used`` 要求笛卡尔放大且结果进结论），
    正向索取会变成"只有犯错的实现才通过"，即惩罚正确行为。
    """
    problems: list[str] = []
    for code in case.expect_quality_codes:
        if code not in codes:
            problems.append(f"缺少质量门禁 code: {code}（实际 {codes}）")
    for code in case.must_not_have_quality_codes:
        if code in codes:
            problems.append(f"出现不应有的质量门禁 code: {code}（实际 {codes}）")
    return problems


def evaluate_case(case: GoldenCase, mode: str, session_id: str,
                  trace_dir) -> CaseOutcome:
    from app.core.agents.data_analyst.graph import run_analysis

    _set_mode(mode)
    # 让本轮 trace 落到独立目录，便于读取且不污染默认 data/traces
    old_dir = tracing_mod._DEFAULT_DIR
    if trace_dir is not None:
        tracing_mod._DEFAULT_DIR = trace_dir
    t0 = time.time()
    try:
        state = run_analysis(session_id, case.query)
    finally:
        if trace_dir is not None:
            tracing_mod._DEFAULT_DIR = old_dir

    out = CaseOutcome(case_id=case.id, status=state.status, error=state.error)
    out.duration_s = round(time.time() - t0, 3)

    # 从结构化 trace 读回真实节点/工具计数与 token
    spans = load_run(session_id, trace_dir=trace_dir)
    spans = spans[:-1] if spans and "spans" in spans[-1] else spans  # 去掉 summary
    stages = [s.get("stage") for s in spans]
    # 工具调用数**从实际执行结果**统计，而不是数 executor span：
    # P1-1 并行执行后整波只发一个 `@trace("executor")` span，数 span 会把
    # 5 次工具调用记成 1 次（指标失真而非功能故障）。state.tool_results 是事实来源。
    out.tool_calls = len(state.tool_results or [])
    out.llm_calls = sum(1 for s in stages if s in _LLM_STAGES)
    out.prompt_tokens = sum(int(s.get("prompt_tokens") or 0) for s in spans)
    out.completion_tokens = sum(int(s.get("completion_tokens") or 0) for s in spans)

    out.findings = len(state.analysis.findings) if state.analysis else 0
    out.reflect_decision = state.reflection.decision if state.reflection else None
    out.report_text = state.report or ""
    out.report_len = len(out.report_text)

    # 过程数据：实际工具集合与成功率（工具轨迹断言）
    executed = list(state.tool_results or [])
    out.executed_tools = sorted({getattr(r, "tool", "") for r in executed} - {""})
    out.tool_success = sum(1 for r in executed if getattr(r, "status", "") == "SUCCESS")
    # E6/03：**未执行**（`skipped`）必须从"失败"里摘出来。否则 D54 那版基线
    # 44/61 条"依赖步骤未完成"会把工具成功率压到 0.228 —— 那个数不是质量。
    out.tool_skipped = sum(1 for r in executed if getattr(r, "skipped", False))
    out.tool_fail = sum(1 for r in executed
                        if getattr(r, "status", "") in ("FAILED", "PARTIAL")
                        and not getattr(r, "skipped", False))

    # 确定性断言
    out.assertions_ok = True
    out.status, _terminal_failures = resolve_terminal(case, state.status, state.error)
    if _terminal_failures:
        out.assertions_ok = False
        out.failed_assertions.extend(_terminal_failures)
    # 被接受的反问终态：report/findings 都是空的，内容断言必然假红 → 早退跳过。
    if out.status == "CLARIFY_OK":
        out.skipped_reason = "判断型问题：反问澄清属可接受终态（accept_clarify）"
        return out
    findings = state.analysis.findings if state.analysis else []
    analysis = state.analysis
    # E6/01：haystack 必须包含**披露字段**。此前只看 report+findings →
    # 写在 limitations / quality_notes 里的"必须标注"要么假绿（模板恰好渲染）要么在 real 模式假红。
    texts = [out.report_text.lower()] + [
        str(getattr(f, "finding", "")).lower() for f in findings
    ]
    texts += [str(x).lower() for x in (getattr(analysis, "limitations", None) or [])]
    texts += [str(x).lower() for x in (getattr(analysis, "quality_notes", None) or [])]
    haystack = " ".join(texts)
    for token in case.must_find:
        if token.lower() not in haystack:
            out.assertions_ok = False
            out.failed_assertions.append(f"报告/发现未命中关键片段: {token}")
    for tool in case.expected_tools:
        if tool not in out.executed_tools:
            out.assertions_ok = False
            out.failed_assertions.append(f"期望调用工具 {tool}，实际执行 {out.executed_tools}")
    for banned in case.must_not_appear:
        if banned.lower() in haystack:
            out.assertions_ok = False
            out.failed_assertions.append(f"出现不应存在的内容: {banned}")
    # --- E6/01：分析师能力断言（读结构化字段，不做字符串匹配） ---
    out.quality_codes = [str(i.get("code")) for i in (state.metadata or {}).get("gate_issues", [])]
    cal = getattr(getattr(state, "reflection", None), "caliber_comparability", None)
    out.caliber_kinds = [str(getattr(i, "kind", "")) for i in (getattr(cal, "issues", None) or [])]
    adversarial = getattr(getattr(state, "reflection", None), "adversarial", None)
    out.refused = bool(getattr(adversarial, "refused", False)) or bool(
        getattr(analysis, "quality_notes", None))

    for msg in quality_code_violations(case, out.quality_codes):
        out.assertions_ok = False
        out.failed_assertions.append(msg)
    for kind in case.expect_caliber_kinds:
        if kind not in out.caliber_kinds:
            out.assertions_ok = False
            out.failed_assertions.append(f"缺少口径问题 kind: {kind}（实际 {out.caliber_kinds}）")
    if case.expect_refusal and not out.refused:
        out.assertions_ok = False
        out.failed_assertions.append("用户要求忽略数据质量，但报告未做任何质量声明（静默遵从）")
    if case.must_have_limitations and not (
            getattr(analysis, "limitations", None) or getattr(analysis, "quality_notes", None)):
        out.assertions_ok = False
        out.failed_assertions.append("报告缺少 limitations / 质量说明")
    for msg in findings_violations(case, len(findings)):
        out.assertions_ok = False
        out.failed_assertions.append(msg)
    # E6/02：报告正文的大额数值必须能在工具结果/证据里找到出处。
    # 这是**唯一**抓得住真基线里"编出一整张区域营收表却判 ✅"的断言——
    # 溯源（unresolved_numeric_claims）只看 findings，正文从不检查。
    out.ungrounded_numbers = ungrounded_numbers(out.report_text, state.tool_results, findings)
    for msg in grounding_violations(case, out.ungrounded_numbers):
        out.assertions_ok = False
        out.failed_assertions.append(msg)
    # --- #5 LLM-judge：评判「答案对不对」（语义层），而非仅字段命中 ---
    # 离线 rubric（结构性代理）默认即可跑；`JUDGE_USE_LLM=1` 且配 key 时走真 LLM 语义评分。
    try:
        from .judge import judge_case as _judge_case

        findings_text = "\n".join(str(getattr(f, "finding", "")) for f in findings)
        _jr = _judge_case(case, out.report_text, findings_text)
        out.judge_score = _jr.score
        out.judge_method = _jr.method
        out.judge_rationale = _jr.rationale
        if case.judge_min_score > 0 and _jr.score < case.judge_min_score:
            out.assertions_ok = False
            out.failed_assertions.append(
                f"LLM-judge 分 {_jr.score} 低于阈值 {case.judge_min_score}（{_jr.method}）")
    except Exception as exc:
        out.judge_rationale = f"judge 异常: {exc}"
    # E1 溯源维度：数值 evidence 必须能解析到真实 SQL step
    try:
        from app.core.agents.data_analyst.sources import trace_counts, unresolved_numeric_claims

        out.numeric_claims, out.traced_claims = trace_counts(findings, list(state.tool_results or []))
        issues = unresolved_numeric_claims(findings, list(state.tool_results or []))
        for msg in issues[:5]:
            out.assertions_ok = False
            out.failed_assertions.append(msg)
        # **补 E1 的 vacuous 漏洞**：`unresolved_numeric_claims` 是"每个数值 claim 都要可溯源"，
        # 当 claim **一个都没有**时它恒真。D38 真实基线 `溯源 0/0` 却全程判过，
        # 于是"分析没给出任何数据结论"这件事测不出来。见 min_numeric_claims 的说明。
        for msg in numeric_claim_violations(case, out.numeric_claims):
            out.assertions_ok = False
            out.failed_assertions.append(msg)
    except Exception:
        pass  # 溯源校验故障不致命，仅不追加

    # --- 测量诚信：识别本轮降级，real 模式下剔除 --------------------------- #
    # 事故背景：OpenRouter 免费额度耗尽 → 每次调用 429 → `router` **静默降级为
    # MockLLM** → 流水线照常产出一份模板报告。此前 runner **完全不读**
    # `state.metadata["degraded"]`，于是"真实基线"里可以混着 mock 的输出，
    # 分值看起来照常，人却无法从报告里看出这批数字根本不是真模型给的
    # —— 这直接违背项目自己的铁律 6（"无 key 前不得声称已达标"）。
    # 现在：降级即**不计分**，并在报告里点名是哪个阶段降的。
    events = state.metadata.get("llm_fallbacks") or []
    out.degraded = bool(events) or bool(state.metadata.get("degraded"))
    if out.degraded and isinstance(events, list):
        stages = []
        for e in events:
            name = (e.get("stage") or e.get("event") or e.get("where")) if isinstance(e, dict) else e
            if name:
                stages.append(str(name))
        out.degraded_stages = sorted(set(stages))
    if out.degraded and mode == "real":
        out.status = "DEGRADED"
        out.assertions_ok = False
        out.failed_assertions.insert(
            0,
            "本轮发生 LLM 降级（"
            f"{'、'.join(out.degraded_stages) or '未知阶段'}）→ 结果非真实模型表现，"
            "已**从真实基线剔除**（重跑前请检查 API 额度/限流）")
    return out


def evaluate(mode: str = "mock", trace_dir=None, *,
             only_real: bool = False, ids: list[str] | None = None,
             badcase_dir=None) -> dict[str, Any]:
    """Run every golden case; return aggregated report dict.

    ``only_real``：只跑 `requires_real=True` 的用例（真实模型下跑全量很贵很慢——
    实测单次 LLM 调用 146s，7 条用例 ≈ 1.5 小时；先拿这部分基线）。
    ``ids``：只跑指定用例（调试单个失败）。
    """
    if mode == "real" and not get_settings().llm_api_key:
        raise RuntimeError("real 模式需要 LLM_API_KEY（设置 .env 或环境变量）")

    cases = [c for c in GOLDEN
             if (not only_real or c.requires_real)
             and (not ids or c.id in ids)]
    if not cases:
        raise RuntimeError(f"没有匹配的用例（only_real={only_real}, ids={ids}）")

    outcomes: list[CaseOutcome] = []
    for case in cases:
        # E6/01：需要真实模型才能验证的用例在 mock 下**跳过并显式计数**，
        # 绝不把"没跑"算进通过率（铁律 6：无 key 前不得声称已达标）。
        if case.requires_real and mode != "real":
            out = CaseOutcome(case_id=case.id, status="SKIPPED",
                              skipped_reason="requires_real：mock 无法验证，需 LLM_API_KEY")
            out.assertions_ok = False
            outcomes.append(out)
            continue
        sid = f"eval_{case.id[:20]}_{uuid.uuid4().hex[:6]}"
        # **单用例隔离**：一个用例因畸形模型输出/上游异常而抛错，绝不能带走整次评测。
        # 实测教训：免费模型给 planner 吐了 `{"id":"step_0"}` → ValidationError 裸穿，
        # 整跑退出码 1，**已跑完的 6 条基线全部丢失、不产出任何报告**。
        try:
            outcomes.append(evaluate_case(case, mode, sid, trace_dir))
        except Exception as exc:
            out = CaseOutcome(case_id=case.id, status="ERROR",
                              error=f"{type(exc).__name__}: {exc}")
            out.failed_assertions.append(f"用例执行抛异常（已被隔离，不影响其它用例）: {exc}")
            outcomes.append(out)

    n = len(outcomes)
    skipped = [o for o in outcomes if o.status == "SKIPPED"]
    # 降级用例与"跳过"同级：**都不可计入真实基线**（降级 = 拿 mock 冒充真模型）
    degraded_cases = [o for o in outcomes if o.status == "DEGRADED"]
    scored = [o for o in outcomes if o.status not in ("SKIPPED", "DEGRADED")]
    finished = [o for o in scored if o.status == "FINISH"]
    tool_calls = sum(o.tool_calls for o in scored)
    tool_success_n = sum(o.tool_success for o in scored)
    tool_fail_n = sum(o.tool_fail for o in scored)
    tool_skipped_n = sum(o.tool_skipped for o in scored)
    numeric_claims_n = sum(o.numeric_claims for o in scored)
    traced_claims_n = sum(o.traced_claims for o in scored)
    reflect_pass = sum(1 for o in scored if o.reflect_decision == "PASS")
    llm_calls = sum(o.llm_calls for o in scored)
    judged = [o for o in scored if o.judge_score is not None]
    judge_scores = [o.judge_score for o in judged]
    judge_avg = round(sum(judge_scores) / len(judge_scores), 3) if judge_scores else None
    judge_method = judged[0].judge_method if judged else None

    # 从 trace 读回真实 token（网关 usage 上报到 span）
    prompt_tokens = sum(o.prompt_tokens for o in outcomes)
    completion_tokens = sum(o.completion_tokens for o in outcomes)
    st = get_settings()
    pi, po = st.cost_input_per_mtok, st.cost_output_per_mtok
    cost_usd = compute_cost_usd(pi, po, prompt_tokens, completion_tokens)
    # E7：每条用例单独算成本 —— 真实基线里定位"谁烧的钱"。
    # 单点扣在聚合之后，pi/po 已 bind，且 SKIPPED/CLARIFY_OK（0 token）会得到 None（= 未计），符合语义。
    for o in outcomes:
        o.cost_usd = compute_cost_usd(pi, po, o.prompt_tokens, o.completion_tokens)

    report = {
        "mode": mode,
        "cases": n,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": {
            "finish_rate": round(len(finished) / len(scored), 3) if scored else 0.0,
            "pass_rate": round(sum(o.assertions_ok for o in scored) / len(scored), 3) if scored else 0.0,
            "skipped_requires_real": len(skipped),
            "degraded_excluded": len(degraded_cases),
            "scored_cases": len(scored),
            # 判断型问题的"反问澄清"是**可接受终态**，但它是"没做分析"：
            # 单独计数，绝不混进 FINISH 率（否则会虚高）。
            "clarify_accepted": sum(1 for o in scored if o.status == "CLARIFY_OK"),
            "tool_calls_total": tool_calls,
            "avg_tool_calls": round(tool_calls / len(scored), 2) if scored else 0.0,
            # E6/03：分母只算**真的执行过**的调用（`success + fail`，跳过不算）。
            # 一条都没执行过 → **None（未定义）**，不是 0 —— 与 `hallucination_rate`
            # 零 claim 同一条纪律：把"没测到"报成 0 是最典型的自欺。
            "tool_success_rate": round(tool_success_n / (tool_success_n + tool_fail_n), 3)
            if (tool_success_n + tool_fail_n) else None,
            # 跳过数必须单独可见：否则读者不知道分母为什么比 `tool_calls_total` 小
            "tool_skipped_total": tool_skipped_n,
            "numeric_claims_total": numeric_claims_n,
            "traced_claims_total": traced_claims_n,
            "traceability_rate": round(traced_claims_n / numeric_claims_n, 3)
            if numeric_claims_n else None,
            # D41 幻觉率监控：与 traceability_rate **同源**（1 − 覆盖率）。
            # 零 claim → None（未定义），绝不报成"0 幻觉"。
            "hallucination_rate": hallucination_rate(numeric_claims_n, traced_claims_n),
            "llm_calls_total": llm_calls,
            "avg_llm_calls": round(llm_calls / len(scored), 2) if scored else 0.0,
            "prompt_tokens_total": prompt_tokens,
            "completion_tokens_total": completion_tokens,
            "tokens_total": prompt_tokens + completion_tokens,
            "cost_estimate_usd": cost_usd,
            "reflect_pass_rate": round(reflect_pass / n, 3),
            "judge_avg_score": judge_avg,
            "judge_method": judge_method,
            "judge_cases": len(judged),
            "avg_report_len": round(sum(o.report_len for o in outcomes) / n, 1),
            "avg_duration_s": round(sum(o.duration_s for o in outcomes) / n, 3),
        },
        "cases_detail": [o.to_dict() for o in outcomes],
        "cost_note": "tokens 由网关 usage 真实上报；cost 需在 config 配置 cost_input/output_per_mtok。",
    }
    report["gates"] = compute_gates(report["metrics"],
                                    sum(len(o.ungrounded_numbers) for o in outcomes),
                                    mode=mode)
    if badcase_dir:
        # D39：把本轮失败的用例落盘，供 `python -m app.eval.badcase --replay` 复跑。
        # 只记"该记的"（SKIPPED / DEGRADED 不算）——见 badcase._is_badcase。
        try:
            from .badcase import record_from_report

            record_from_report(report, badcase_dir)
        except Exception:  # 落盘失败不得影响评测本身
            pass
    return report


def _verdict(gate: dict | None) -> str:
    """门禁结论渲染：缺该项 → `—`（**不是 PASS**，缺项不等于通过）。"""
    if not gate:
        return "—"
    return "PASS" if gate.get("passed") else "**FAIL**"


def _fmt_rate(value: float | None) -> str:
    """比率渲染：**`None` = 未定义（分母为 0）**，必须说出来，不能印成 0。

    与 `hallucination_rate` 同一条纪律：把"没测到"印成 0 是最典型的自欺。
    """
    return "未定义（无有效分母）" if value is None else str(value)


def _fmt_cost(value: float | None) -> str:
    """成本渲染：`None`（没配单价）与 `0.0`（确实免费）**必须长得不一样**。

    真实基线报告里那行 `成本 USD 0.0` 其实是**未计**——`.env` 里 `COST_*=0`
    把"没配"表达成了"免费"。不猜单价，但也不能让读者把它读成零成本。
    """
    if value is None:
        return "未计（未配单价）"
    return f"{value}（单价为 0 = 已知免费）" if value == 0 else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    m = report["metrics"]
    lines = [
        "# Eval 报告",
        "",
        f"- 模式：{report['mode']}　用例数：{report['cases']}　时间：{report['generated_at']}",
        "",
        "## 指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
    ]
    rows = [
        ("FINISH 率", m.get("finish_rate")), ("断言通过率", m.get("pass_rate")),
        ("平均工具调用", m.get("avg_tool_calls")),
        ("工具成功率", _fmt_rate(m.get("tool_success_rate"))),
        # E6/03：分母变了就必须让读者看见为什么 —— "跳过"不是失败，是**没轮到**
        ("其中：跳过（依赖未满足，未执行）", m.get("tool_skipped_total", 0)),
        ("平均 LLM 调用", m.get("avg_llm_calls")),
        ("Reflection PASS 率", m.get("reflect_pass_rate")),
        ("LLM-judge 平均分", m.get("judge_avg_score")),
        ("LLM-judge 方法", m.get("judge_method")),
        ("平均报告长度", m.get("avg_report_len")),
        ("平均耗时(s)", m.get("avg_duration_s")), ("prompt tokens", m.get("prompt_tokens_total")),
        ("completion tokens", m.get("completion_tokens_total")),
        ("总 tokens", m.get("tokens_total")),
        ("成本 USD", _fmt_cost(m.get("cost_estimate_usd"))),
        ("溯源覆盖率", _fmt_rate(m.get("traceability_rate"))),
        ("溯源 claims", f"{m.get('traced_claims_total')}/{m.get('numeric_claims_total')}"),
        # D41：疑似幻觉率 = 1 − 覆盖率。**None = 未定义（零数值结论）**，不是 0。
        ("**疑似幻觉率**（数值无源占比）", _fmt_rate(m.get("hallucination_rate"))),
        # 跳过项必须显式可见：否则"没跑"会被误读成"通过"
        ("跳过（需真实模型）", m.get("skipped_requires_real", 0)),
        # 降级项同理，且更危险：它是"拿 mock 冒充真模型"，不发出来就会被当真实成绩
        ("**降级剔除（非真实模型产出）**", m.get("degraded_excluded", 0)),
        # 判断型问题的反问是可接受终态，但**不是"做对了"** → 单独可见
        ("其中：判断型问题被接受的澄清", m.get("clarify_accepted", 0)),
        ("实际计分用例数", m.get("scored_cases", report["cases"])),
    ]
    for name, val in rows:
        lines.append(f"| {name} | {val} |")
    gates = report.get("gates")
    # 渲染器必须**不能崩**：报告是产物，缺一个门禁键就 `KeyError` 会把整篇报告打掉，
    # 比少印一行糟得多（E6/03 的 `_report_with_cost` 就构造了最小 gates）。
    if gates:
        ev = gates.get("evidence")
        ev_note = "—"
        if ev:
            # 跳过项在 mock 下**按设计发生**，不当否决理由——否则 `--mode mock --strict`
            # 永远是退出码 2，门禁变成噪音。（`real` 模式下的跳过仍然否决：铁律 6。）
            ev_note = (f"跳过 {ev.get('skipped')}"
                       + ("（mock 按设计，不否决）" if not ev.get("skipped_counts", True) else "")
                       + f" / 降级 {ev.get('degraded')}")
        halu = gates.get("hallucination") or {}
        ground = gates.get("grounded_numbers") or {}
        lines += [
            "",
            "## 门禁（E6/02）",
            "",
            "| 门禁 | 判据 | 现值 | 结论 |",
            "|---|---|---|---|",
            f"| 幻觉 | `hallucination_rate <= 0` | {halu.get('value', '—')} | "
            f"{_verdict(halu)} |",
            f"| 正文数值溯源 | 无源大额数值 = 0 | {ground.get('violations', '—')} 条 | "
            f"{_verdict(ground)} |",
            f"| 证据完整性 | 无降级；`real` 下另须无跳过 | {ev_note} | "
            f"{_verdict(ev)} |",
            "",
            f"**总体：{'PASS' if gates.get('passed') else 'FAIL'}**"
            + ("（`--strict` 下退出码 2）" if not gates.get("passed") else ""),
            "",
            "> 幻觉率 `None` = 零数值结论（**未定义**，不是'零幻觉'），不否决；"
            "但'没有数值结论'本身由 `min_numeric_claims` 在用例级拦。",
        ]
    if m.get("degraded_excluded"):
        deg = [c["case_id"] for c in report["cases_detail"] if c["status"] == "DEGRADED"]
        stages = sorted({s for c in report["cases_detail"] for s in c.get("degraded_stages", [])})
        lines += [
            "",
            f"> ⚠️ **本轮有 {len(deg)} 条用例发生 LLM 降级，已从真实基线剔除**："
            f"{'、'.join(deg)}",
            f"> 降级阶段：{'、'.join(stages) or '未知'}。"
            "这些用例的产出其实是 **Mock 模板**，不代表真实模型能力。",
            "> 常见原因：上游 429 限流 / 余额不足 / 模型不可用。"
            "**请先解决额度问题再重跑**，否则基线不可用。",
        ]
    lines += ["", "## 用例明细", "", "| id | status | tools | llm | findings | refl | cost | assert | err |", "|---|---|---|---|---|---|---|---|---|"]
    for c in report["cases_detail"]:
        mark = {"SKIPPED": "⏭", "DEGRADED": "⚠️"}.get(c["status"],
                                                     "✅" if c["assertions_ok"] else "❌")
        cost_cell = _fmt_cost(c.get("cost_usd"))
        lines.append(
            f"| {c['case_id']} | {c['status']} | {c['tool_calls']} | {c['llm_calls']} | "
            f"{c['findings']} | {c['reflect_decision']} | {cost_cell} | {mark} | "
            f"{(c.get('skipped_reason') or str(c['failed_assertions']))[:40]} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Data Analyst Agent eval runner")
    ap.add_argument("--mode", choices=["mock", "real"], default="mock")
    ap.add_argument("--out", default=None, help="写 markdown 报告到此路径")
    ap.add_argument("--only-real", action="store_true",
                    help="只跑 requires_real 用例（真实模型下省时间/成本）")
    ap.add_argument("--ids", default="", help="只跑指定用例 id（逗号分隔）")
    ap.add_argument("--badcases", default=None,
                    help="把失败用例落盘到此目录（D39；默认不落盘）")
    ap.add_argument("--strict", action="store_true",
                    help="门禁失败时以退出码 2 结束（E6/02；默认不改退出码）")
    args = ap.parse_args()

    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    report = evaluate(args.mode, only_real=args.only_real, ids=ids or None,
                      badcase_dir=args.badcases)
    md = render_markdown(report)
    print(md)
    if args.out:
        import pathlib
        p = pathlib.Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        print(f"\n[report] -> {p}")
    # 简洁 JSON 行，便于脚本化读取
    print("\n[metrics-json]", report["metrics"])

    # 门禁（E6/02）：**只在显式 `--strict` 下改退出码**。
    # 既有脚本/CI 依赖"跑完即 0"，静默改语义会让它们在不该红的地方红。
    if args.strict and not (report.get("gates") or {}).get("passed", True):
        print("\n[gates] FAIL —— `--strict` 下退出码 2", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
