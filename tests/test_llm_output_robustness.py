"""模型结构化输出的健壮性：畸形 JSON **不得打挂整条链路 / 整次评测**。

## 触发这一组用例的真实事故（2026-09-12）

用新演示数据集重跑真 LLM eval 时，免费模型给 planner 吐了一个**退化步骤**：

```json
{"steps": [ ... , {"id": "step_0"} ]}
```

`PlanStep` 的 ``objective``/``action``/``tool`` 是必填 → ``PlanModel.model_validate``
抛 ``ValidationError`` → **裸穿透** ``run_analysis`` → 进程退出码 1：

- 已跑完的用例**全部丢失**；
- 一份报告都没产出（``--out`` 文件未生成）。

这不是"模型不听话"的一次性意外 —— 真实/免费/小模型产出结构不合法 JSON 是**常态**，
代码库里 ``PlanModel._coerce`` / ``AnalysisResult._coerce_lists`` 的注释早已承认这一点
（"整跑失败"），只是 ``PlanStep`` 的必填字段没人兜、外层也没有最后一道网。

本文件守四层：
1. **模型层**：退化步骤被丢弃（可审计），半残步骤被补全 —— 而不是抛 ValidationError；
2. **节点层**：``_llm_model`` 把字段级错误**回喂**重试一次，仍失败才降级/报错；
3. **编排层**：``run_analysis`` 把任何节点异常收敛成 ``status=ERROR``；
4. **评测层**：runner 单用例隔离，一个用例炸不影响其它用例与报告产出。
"""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst import nodes as N
from app.core.agents.data_analyst.state import (
    AnalysisResult,
    ContextModel,
    ModelOutputError,
    PlanModel,
)


# --------------------------------------------------------------------------- #
# 1. 模型层：畸形/半残步骤的容错
# --------------------------------------------------------------------------- #
def test_degenerate_step_is_dropped_not_fatal():
    """只剩 id 的退化条目 → 丢弃（而不是硬塞默认值发一次无意义工具调用）。"""
    plan = PlanModel.model_validate({
        "goal": "g",
        "steps": [
            {"id": "s1", "objective": "取数", "action": "查询", "tool": "sql_query"},
            {"id": "step_0"},  # ← 事故现场：模型退化的那一项
            {"id": "s2", "objective": "画像", "action": "profiling", "tool": "dataset_profile"},
        ],
    })
    assert [s.id for s in plan.steps] == ["s1", "s2"]
    assert (plan.raw or {}).get("_dropped_steps") == 1, "丢弃要可审计，不能静默"


def test_partially_specified_step_is_completed():
    """有 objective 但缺 action/tool 的步骤 → 补全，保留而不是丢弃。"""
    plan = PlanModel.model_validate({
        "steps": [{"objective": "看各区域营收", "action": "聚合查询"}],
    })
    assert len(plan.steps) == 1
    assert plan.steps[0].tool == "sql_query", "缺 tool 时兜底成 sql_query"
    assert plan.steps[0].objective == "看各区域营收"


def test_step_id_is_generated_when_missing():
    plan = PlanModel.model_validate({"steps": [{"objective": "x", "action": "y", "tool": "sql_query"}]})
    assert plan.steps[0].id


def test_all_degenerate_steps_yield_empty_plan_not_exception():
    """全部退化 → 空计划（由节点层判断并响亮失败），但**模型层不抛**。"""
    plan = PlanModel.model_validate({"steps": [{"id": "step_0"}, {"id": "step_1"}]})
    assert plan.steps == []
    assert (plan.raw or {}).get("_dropped_steps") == 2


def test_string_steps_still_work():
    """既有行为不能回归：字符串步骤转成最小可用步骤。"""
    plan = PlanModel.model_validate({"steps": ["查一下各区域营收"]})
    assert len(plan.steps) == 1
    assert plan.steps[0].tool == "sql_query"


# --------------------------------------------------------------------------- #
# 2. 节点层：_llm_model 回喂重试
# --------------------------------------------------------------------------- #
def test_llm_model_retries_with_schema_feedback(monkeypatch):
    """第一次输出不可用 → 第二次的提示词里必须带上**具体原因**（不是原样重试）。"""
    prompts: list[str] = []
    outputs = [
        '{"steps": [{"id": "step_0"}]}',                       # 退化：全被丢弃 → 0 步
        '{"steps": [{"objective": "取数", "action": "查询"}]}', # 合法（tool 可补）
    ]

    def fake_llm(stage, user, json_mode=True):
        prompts.append(user)
        return outputs[min(len(prompts) - 1, len(outputs) - 1)]

    monkeypatch.setattr(N, "_llm", fake_llm)
    model, err = N._llm_model(PlanModel, "planner", "原始提示词", ok=lambda p: bool(p.steps))

    assert err is None, "第二次已可用，不应报降级"
    assert len(model.steps) == 1
    assert len(prompts) == 2, "应恰好重试一次"
    assert "原始提示词" in prompts[1]
    assert "上一次输出不可用" in prompts[1], "重试必须回喂具体原因，否则等于原样重试"


def test_valid_but_unusable_triggers_retry(monkeypatch):
    """**关键回归**：字段都有默认值 ⇒ 空对象也能过 pydantic 校验。

    只靠"校验是否抛错"判成败会让重试逻辑形同虚设（空计划被当成成功）。
    必须由调用方用 ``ok`` 表达内容层面的可用性。
    """
    calls = []
    monkeypatch.setattr(N, "_llm", lambda *a, **k: calls.append(1) or "{}")

    # 不加 ok：校验通过 → 直接成功（这正说明"校验"不足以判可用）
    model, err = N._llm_model(PlanModel, "planner", "q")
    assert err is None and model.steps == [] and len(calls) == 1

    # 加 ok：空计划被判不可用 → 重试一次 → 仍不可用 → 报错
    calls.clear()
    with pytest.raises(ModelOutputError):
        N._llm_model(PlanModel, "planner", "q", ok=lambda p: bool(p.steps))
    assert len(calls) == 2, "不可用必须触发重试"


def test_llm_model_raises_when_still_invalid_without_fallback(monkeypatch):
    monkeypatch.setattr(N, "_llm", lambda *a, **k: "{}")  # 永远空对象
    with pytest.raises(ModelOutputError) as ei:
        N._llm_model(PlanModel, "planner", "q", ok=lambda p: bool(p.steps), retries=1)
    assert "planner" in str(ei.value)
    assert "不可用" in str(ei.value)


def test_empty_content_is_reported_as_empty_not_schema_problem(monkeypatch):
    """**空内容 ≠ "字段为空"**：必须报出真正的原因，否则排查会走错方向。

    真实踩坑：glm-5.3（推理模型）在 planner 的大提示词下返回**空串**——
    max_tokens 被 reasoning 占满、正文没吐出来。此时 `parsed == {}`，
    而本套 schema 字段几乎都有默认值 ⇒ `model_validate({})` **成功** ⇒
    旧实现报"输出结构合法但内容不可用：关键字段为空（首 400 字符：）"，
    **首 400 字符是空的**，看不出是模型没输出、还是模型输出了空 JSON。
    """
    monkeypatch.setattr(N, "_llm", lambda *a, **k: "")  # 模型什么都没吐

    with pytest.raises(ModelOutputError) as ei:
        N._llm_model(PlanModel, "planner", "q", ok=lambda p: bool(p.steps), retries=1)

    msg = str(ei.value)
    assert "空内容" in msg, msg
    assert "LLM_MAX_TOKENS" in msg, "应提示调大 token 预算（推理模型占满是常见原因）"


def test_whitespace_only_content_also_treated_as_empty(monkeypatch):
    monkeypatch.setattr(N, "_llm", lambda *a, **k: "   \n  ")
    with pytest.raises(ModelOutputError) as ei:
        N._llm_model(PlanModel, "planner", "q", ok=lambda p: bool(p.steps), retries=1)
    assert "空内容" in str(ei.value)



    monkeypatch.setattr(N, "_llm", lambda *a, **k: "{}")
    model, err = N._llm_model(
        ContextModel, "context", "q",
        ok=lambda c: bool(c.objective),
        fallback=lambda parsed, _exc: ContextModel(raw=parsed or {}))
    assert isinstance(model, ContextModel)
    assert err is not None and "context" in err, "降级必须留下说明（供披露）"


def test_llm_model_does_not_retry_on_success(monkeypatch):
    calls = []

    def fake_llm(*a, **k):
        calls.append(1)
        return '{"steps": [{"objective": "o", "action": "a", "tool": "sql_query"}]}'

    monkeypatch.setattr(N, "_llm", fake_llm)
    _, err = N._llm_model(PlanModel, "planner", "q", ok=lambda p: bool(p.steps))
    assert err is None and len(calls) == 1, "可用输出不应多花一次 LLM 调用"


def test_analyst_fallback_never_crashes(monkeypatch):
    monkeypatch.setattr(N, "_llm", lambda *a, **k: "not json at all")
    model, err = N._llm_model(AnalysisResult, "analyst", "q",
                              ok=lambda a: bool(a.findings),
                              fallback=lambda parsed, _e: AnalysisResult(raw=parsed or {}))
    assert isinstance(model, AnalysisResult) and err


# --------------------------------------------------------------------------- #
# 3. 编排层：任何节点异常都收敛成 status=ERROR
# --------------------------------------------------------------------------- #
def test_run_analysis_converts_node_exception_to_error(monkeypatch):
    from app.core.agents.data_analyst import graph

    def boom(_state):
        raise ModelOutputError("planner 产出的计划没有任何可用步骤")

    monkeypatch.setattr(graph, "_drive_sync", boom)
    monkeypatch.setenv("MOCK_LLM", "true")
    from app.config import get_settings
    get_settings.cache_clear()

    state = graph.run_analysis("sess_boom_1", "分析营收")
    assert state.status == "ERROR", "节点异常必须收敛成 ERROR，而不是穿透调用方"
    assert "ModelOutputError" in (state.error or "")
    assert state.metadata.get("aborted_by_exception") == "ModelOutputError"


# --------------------------------------------------------------------------- #
# 4. 评测层：单用例隔离
# --------------------------------------------------------------------------- #
def test_eval_isolates_failing_case(monkeypatch, tmp_path):
    """一个用例抛异常 → 记为 ERROR，其余用例照常跑完，**报告仍产出**。"""
    from app.eval import runner
    from app.eval.golden import GoldenCase

    cases = (
        GoldenCase(id="ok_1", query="正常用例一"),
        GoldenCase(id="boom", query="会抛异常的用例"),
        GoldenCase(id="ok_2", query="正常用例二"),
    )
    monkeypatch.setattr(runner, "GOLDEN", cases)

    ran: list[str] = []

    def fake_eval(case, mode, sid, trace_dir):
        ran.append(case.id)
        if case.id == "boom":
            raise ModelOutputError("模拟畸形模型输出")
        out = runner.CaseOutcome(case_id=case.id, status="FINISH")
        out.assertions_ok = True
        return out

    monkeypatch.setattr(runner, "evaluate_case", fake_eval)

    report = runner.evaluate("mock", only_real=False)
    by_id = {c["case_id"]: c for c in report["cases_detail"]}

    assert ran == ["ok_1", "boom", "ok_2"], "异常用例不得中断遍历"
    assert set(by_id) == {"ok_1", "boom", "ok_2"}, "报告要包含全部用例"
    assert by_id["boom"]["status"] == "ERROR"
    assert by_id["ok_1"]["assertions_ok"] and by_id["ok_2"]["assertions_ok"]

    md = runner.render_markdown(report)
    assert "boom" in md and "ok_2" in md, "报告文本必须完整产出"


def test_eval_report_written_even_with_failures(monkeypatch, tmp_path):
    """端到端：含异常用例时 ``--out`` 仍能写出文件（事故里就是这一步没发生）。"""
    from app.eval import runner
    from app.eval.golden import GoldenCase

    monkeypatch.setattr(runner, "GOLDEN", (GoldenCase(id="boom", query="x"),))
    monkeypatch.setattr(runner, "evaluate_case",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))

    out = tmp_path / "eval.md"
    report = runner.evaluate("mock", only_real=False)
    out.write_text(runner.render_markdown(report), encoding="utf-8")

    assert out.exists() and "boom" in out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 5. 评测诚信：降级（拿 mock 冒充真模型）**必须被识别并剔除**
# --------------------------------------------------------------------------- #
def _fake_state(degraded: bool, status: str = "FINISH"):
    from app.core.agents.data_analyst.state import AgentState

    st = AgentState(session_id="deg_sess", user_query="各区域营收")
    st.status = status
    st.report = "一份看起来正常的报告"
    st.metadata["llm_fallbacks"] = (
        [{"stage": "planner"}, {"stage": "reporter"}] if degraded else [])
    st.metadata["degraded"] = degraded
    return st


def _run_case(monkeypatch, mode: str, degraded: bool):
    from app.core.agents.data_analyst import graph
    from app.eval import runner
    from app.eval.golden import GoldenCase

    monkeypatch.setattr(graph, "run_analysis", lambda sid, q: _fake_state(degraded))
    # judge 用离线 rubric，避免测试打网络
    monkeypatch.setenv("MOCK_LLM", "true")
    case = GoldenCase(id="deg_case", query="各区域营收")
    return runner.evaluate_case(case, mode, "deg_sess", None)


def test_real_mode_marks_degraded_case_and_excludes_from_baseline(monkeypatch):
    """事故现场：额度耗尽 → 429 → 静默降级 mock → 旧 runner **照常计分**。"""
    out = _run_case(monkeypatch, "real", degraded=True)
    assert out.degraded is True
    assert out.status == "DEGRADED", "降级必须改状态，不能混进真实结果"
    assert out.assertions_ok is False
    assert any("降级" in a for a in out.failed_assertions)
    assert set(out.degraded_stages) == {"planner", "reporter"}, out.degraded_stages


def test_real_mode_clean_run_is_not_marked_degraded(monkeypatch):
    out = _run_case(monkeypatch, "real", degraded=False)
    assert out.degraded is False
    assert out.status == "FINISH", "没有降级时行为不变"


def test_mock_mode_is_not_marked_degraded(monkeypatch):
    """mock 模式本来就该用模板，不适用"降级"概念。"""
    out = _run_case(monkeypatch, "mock", degraded=True)
    assert out.status == "FINISH"


def test_evaluate_excludes_degraded_from_metrics(monkeypatch):
    from app.eval import runner
    from app.eval.golden import GoldenCase

    monkeypatch.setattr(runner, "GOLDEN", (
        GoldenCase(id="good", query="a"),
        GoldenCase(id="deg", query="b"),
    ))

    def fake_isolated(case, mode, sid, trace_dir):
        out = runner.CaseOutcome(case_id=case.id, status="FINISH")
        out.assertions_ok = True
        if case.id == "deg":
            out.status = "DEGRADED"
            out.degraded = True
            out.degraded_stages = ["planner"]
            out.assertions_ok = False
        return out

    monkeypatch.setattr(runner, "evaluate_case", fake_isolated)
    report = runner.evaluate("real", only_real=False)
    m = report["metrics"]

    assert m["degraded_excluded"] == 1, "降级数必须显式出现在指标里"
    assert m["scored_cases"] == 1, "降级用例不得计入计分口径"
    assert m["pass_rate"] == 1.0, "剔除降级后，只有真实用例参与通过率"

    md = runner.render_markdown(report)
    assert "降级剔除" in md and "deg" in md, "报告必须点名哪些用例被剔除"
    assert "⚠️" in md


# --------------------------------------------------------------------------- #
# 5. 评测诚信：降级（拿 mock 冒充真模型）**必须被识别并剔除**
# --------------------------------------------------------------------------- #
def _fake_state(degraded: bool, status: str = "FINISH"):
    from app.core.agents.data_analyst.state import AgentState

    st = AgentState(session_id="deg_sess", user_query="各区域营收")
    st.status = status
    st.report = "一份看起来正常的报告"
    st.metadata["llm_fallbacks"] = (
        [{"stage": "planner"}, {"stage": "reporter"}] if degraded else [])
    st.metadata["degraded"] = degraded
    return st


def _run_case(monkeypatch, mode: str, degraded: bool):
    from app.core.agents.data_analyst import graph
    from app.eval import runner
    from app.eval.golden import GoldenCase

    monkeypatch.setattr(graph, "run_analysis", lambda sid, q: _fake_state(degraded))
    # judge 用离线 rubric，避免测试打网络
    monkeypatch.setenv("MOCK_LLM", "true")
    case = GoldenCase(id="deg_case", query="各区域营收")
    return runner.evaluate_case(case, mode, "deg_sess", None)


def test_real_mode_marks_degraded_case_and_excludes_from_baseline(monkeypatch):
    """事故现场：额度耗尽 → 429 → 静默降级 mock → 旧 runner **照常计分**。"""
    out = _run_case(monkeypatch, "real", degraded=True)
    assert out.degraded is True
    assert out.status == "DEGRADED", "降级必须改状态，不能混进真实结果"
    assert out.assertions_ok is False
    assert any("降级" in a for a in out.failed_assertions)
    assert set(out.degraded_stages) == {"planner", "reporter"}, out.degraded_stages


def test_real_mode_clean_run_is_not_marked_degraded(monkeypatch):
    out = _run_case(monkeypatch, "real", degraded=False)
    assert out.degraded is False
    assert out.status == "FINISH", "没有降级时行为不变"


def test_mock_mode_is_not_marked_degraded(monkeypatch):
    """mock 模式本来就该用模板，不适用"降级"概念。"""
    out = _run_case(monkeypatch, "mock", degraded=True)
    assert out.status == "FINISH"


def test_evaluate_excludes_degraded_from_metrics(monkeypatch):
    from app.eval import runner
    from app.eval.golden import GoldenCase

    monkeypatch.setattr(runner, "GOLDEN", (
        GoldenCase(id="good", query="a"),
        GoldenCase(id="deg", query="b"),
    ))

    def fake_isolated(case, mode, sid, trace_dir):
        out = runner.CaseOutcome(case_id=case.id, status="FINISH")
        out.assertions_ok = True
        if case.id == "deg":
            out.status = "DEGRADED"
            out.degraded = True
            out.degraded_stages = ["planner"]
            out.assertions_ok = False
        return out

    monkeypatch.setattr(runner, "evaluate_case", fake_isolated)
    report = runner.evaluate("real", only_real=False)
    m = report["metrics"]

    assert m["degraded_excluded"] == 1, "降级数必须显式出现在指标里"
    assert m["scored_cases"] == 1, "降级用例不得计入计分口径"
    assert m["pass_rate"] == 1.0, "剔除降级后，只有真实用例参与通过率"

    md = runner.render_markdown(report)
    assert "降级剔除" in md and "deg" in md, "报告必须点名哪些用例被剔除"
    assert "⚠️" in md
