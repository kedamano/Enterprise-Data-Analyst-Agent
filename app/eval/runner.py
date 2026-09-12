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
import os
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

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "report_text"}
        return d


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
    out.tool_fail = sum(1 for r in executed if getattr(r, "status", "") in ("FAILED", "PARTIAL"))

    # 确定性断言
    out.assertions_ok = True
    if case.expect_finish and state.status != "FINISH":
        out.assertions_ok = False
        out.failed_assertions.append(f"期望 FINISH，实际 {state.status}: {state.error}")
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

    for code in case.expect_quality_codes:
        if code not in out.quality_codes:
            out.assertions_ok = False
            out.failed_assertions.append(f"缺少质量门禁 code: {code}（实际 {out.quality_codes}）")
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
             only_real: bool = False, ids: list[str] | None = None) -> dict[str, Any]:
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
        except Exception as exc:  # noqa: BLE001 — 隔离层必须宽
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
    cost_usd = None
    if (pi > 0 or po > 0) and (prompt_tokens or completion_tokens):
        cost_usd = round(prompt_tokens / 1e6 * pi + completion_tokens / 1e6 * po, 6)

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
            "tool_calls_total": tool_calls,
            "avg_tool_calls": round(tool_calls / len(scored), 2) if scored else 0.0,
            "tool_success_rate": round(tool_success_n / (tool_success_n + tool_fail_n), 3)
            if (tool_success_n + tool_fail_n) else 0.0,
            "numeric_claims_total": numeric_claims_n,
            "traced_claims_total": traced_claims_n,
            "traceability_rate": round(traced_claims_n / numeric_claims_n, 3)
            if numeric_claims_n else None,
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
    return report


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
        ("FINISH 率", m["finish_rate"]), ("断言通过率", m["pass_rate"]),
        ("平均工具调用", m["avg_tool_calls"]), ("工具成功率", m["tool_success_rate"]),
        ("平均 LLM 调用", m["avg_llm_calls"]),
        ("Reflection PASS 率", m["reflect_pass_rate"]),
        ("LLM-judge 平均分", m["judge_avg_score"]),
        ("LLM-judge 方法", m["judge_method"]),
        ("平均报告长度", m["avg_report_len"]),
        ("平均耗时(s)", m["avg_duration_s"]), ("prompt tokens", m["prompt_tokens_total"]),
        ("completion tokens", m["completion_tokens_total"]),
        ("总 tokens", m["tokens_total"]), ("成本 USD", m["cost_estimate_usd"]),
        ("溯源覆盖率", m["traceability_rate"]), ("溯源 claims", f"{m['traced_claims_total']}/{m['numeric_claims_total']}"),
        # 跳过项必须显式可见：否则"没跑"会被误读成"通过"
        ("跳过（需真实模型）", m.get("skipped_requires_real", 0)),
        # 降级项同理，且更危险：它是"拿 mock 冒充真模型"，不发出来就会被当真实成绩
        ("**降级剔除（非真实模型产出）**", m.get("degraded_excluded", 0)),
        ("实际计分用例数", m.get("scored_cases", report["cases"])),
    ]
    for name, val in rows:
        lines.append(f"| {name} | {val} |")
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
    lines += ["", "## 用例明细", "", "| id | status | tools | llm | findings | refl | assert | err |", "|---|---|---|---|---|---|---|---|"]
    for c in report["cases_detail"]:
        mark = {"SKIPPED": "⏭", "DEGRADED": "⚠️"}.get(c["status"],
                                                     "✅" if c["assertions_ok"] else "❌")
        lines.append(
            f"| {c['case_id']} | {c['status']} | {c['tool_calls']} | {c['llm_calls']} | "
            f"{c['findings']} | {c['reflect_decision']} | {mark} | "
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
    args = ap.parse_args()

    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    report = evaluate(args.mode, only_real=args.only_real, ids=ids or None)
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


if __name__ == "__main__":
    main()
