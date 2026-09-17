"""E2/05 · `generate_report` 不能作为计划步骤 —— 一个**按构造做不到**的僵尸步骤。

Spec: docs/specs/E2/05-report-step-not-plannable.md

为什么这个步骤不该存在（三条，都有真实产物支撑）
--------------------------------------------------
① **按构造做不到**：它唯一的输入是 `state.analysis`（`report_tool.run` 只读
   `analysis`/`reflection`/`objective`），而流水线是 **Executor → Analyst**——
   执行器阶段 `state.analysis` 还是默认空值。D54 真实基线（`data/checkpoints/eval_*.json`）：
   13 次 `依赖步骤未完成` + 1 次侥幸执行产出的 **163 字符空壳**（零 findings/指标/建议）。
② **产出没有消费者**：交付物由 `run_reporter` 在分析**之后**写
   （`nodes.py:1589/1603`），eval 读的也是它（`runner.py:384`）。
   所以删掉这个入口**不会让任何人拿不到报告**——§三钉的就是这句话。
③ **它会顶替真实取数步骤**：D54 产物里 `q_channel_trend` 的失败信息是
   `期望调用工具 schema_search，实际执行 ['generate_report', ...]`。

本文件钉四件事
--------------
① 预防：它不再进入 planner 的可见工具集（**含"全给"分支**）；
② 检测：真被排出来就**响亮拒绝**，`skipped=False`（留在失败分母里），且**绝不执行**；
③ 交付物不变量：计划里含报告步骤时，用户**照样**拿到非空的真报告；
④ 别误删：工具本身仍在注册表里、直接调用仍可用（`run_reporter` 的模板兜底要用它）。

边界（**故意如此**）：守卫在**步骤级**（`_run_one_step`），不在 `execute_tool`
——`test_agent_real.py::test_generate_report_runs` 直接调这个工具，必须继续可用；
只有"把它排进计划"这件事是非法的。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.nodes import run_executor, run_executor_all
from app.core.agents.data_analyst.state import (
    AgentState,
    ContextModel,
    PlanModel,
    PlanStep,
)
from app.core.tools.routing import select_tools_for_planner
from app.core.tools.specs import TOOL_SPECS, ToolPermission, ToolSpec

_REPORT_QUERY = "给我一份销售周报，包含结论与建议"


def _state() -> AgentState:
    s = AgentState(session_id="e2_05", user_query="分析各区域营收表现")
    s.context = ContextModel(objective="分析各区域营收", metrics=["营收"])
    return s


def _sql_step(sid: str = "step_1") -> PlanStep:
    return PlanStep(id=sid, objective="取数", action="查询各区域营收", tool="sql_query")


def _report_step(sid: str = "step_9", deps: list[str] | None = None) -> PlanStep:
    return PlanStep(id=sid, objective="出报告", action="汇总成报告",
                    tool="generate_report", dependencies=list(deps or []))


def _synthetic_specs() -> dict[str, ToolSpec]:
    """**小**工具集：两个无关工具 + 真的 `generate_report`。

    用小工具集才能**逼出路由分支**（`threshold_count=0`），从而验证过滤在
    "全给"与"路由"两条路上都生效——真实工具数（9）还没超过阈值 12，
    只测真实集合的话路由那条路根本没被走到。
    """
    def _fake(name: str, desc: str) -> ToolSpec:
        return ToolSpec(name=name, description=desc, permission=ToolPermission.COMPUTE,
                        timeout_s=1, data_scope="test", rate_limit_per_min=1,
                        input_schema={"type": "object", "properties": {}})

    return {
        "fake_alpha": _fake("fake_alpha", "unrelated capability alpha"),
        "fake_beta": _fake("fake_beta", "unrelated capability beta"),
        **{k: v for k, v in TOOL_SPECS.items() if k == "generate_report"},
    }


def _spy_execute_tool(monkeypatch) -> list[str]:
    """记录**真正被执行**的工具名——这是"拒绝"与"执行后失败"的分界。"""
    import app.core.agents.data_analyst.nodes as nodes

    seen: list[str] = []
    real = nodes.execute_tool

    def spy(step_id, tool, params, session_id):
        seen.append(tool)
        return real(step_id, tool, params, session_id)

    monkeypatch.setattr(nodes, "execute_tool", spy)
    return seen


# --------------------------------------------------------------------------- #
# 一、预防：不再进入 planner 的可见工具集
# --------------------------------------------------------------------------- #
def test_absent_from_the_all_tools_branch():
    """工具数 ≤ 阈值 → **全给**分支。这里过滤是唯一让它消失的原因（非平凡）。"""
    names, routed = select_tools_for_planner(_REPORT_QUERY, threshold_count=99)
    assert routed is False, "本节必须走'全给'分支，否则钉不住这条路径"
    assert "generate_report" not in names
    assert len(names) == len(TOOL_SPECS) - 1, "只是少了一个，别把工具集砍小了"
    assert "sql_query" in names and "knowledge_search" in names


def test_absent_from_the_routed_branch_even_for_a_report_query():
    """工具数 > 阈值 → **路由**分支。查询也选成它别名最命中的那句。"""
    names, routed = select_tools_for_planner(_REPORT_QUERY, specs=_synthetic_specs(),
                                             threshold_count=0)
    assert routed is True, "本节必须走'路由'分支"
    assert "generate_report" not in names


def test_the_filter_is_what_removes_it(monkeypatch):
    """**反证**：清空名单 → 同一条查询下它立刻回来。

    说明上面两条不是"查询恰好没命中"侥幸过的：别名
    `报告 周报 月报 汇报 结论 建议 总结` 对这种查询是高命中，过滤才是它消失的原因。
    """
    from app.core.tools import specs as specs_mod

    monkeypatch.setattr(specs_mod, "NOT_PLANNABLE_TOOLS", frozenset())
    names, _ = select_tools_for_planner(_REPORT_QUERY, specs=_synthetic_specs(),
                                        threshold_count=0)
    assert "generate_report" in names, "过滤没在做事，那上面的绿是假的"


def test_planner_prompt_does_not_offer_it():
    """提示词是**始终生效**的那一路——路由故障时会回退全量工具说明。

    所以只做路由侧过滤不够：`planner.md` 里那份清单必须同步去掉，
    并把"只在最后一步用"那句诱导改成**反向**说明。
    """
    text = Path("app/core/prompts/data_analyst/planner.md").read_text(encoding="utf-8")
    assert "generate_report" not in text
    assert "only as the final step" not in text
    assert "Reporter" in text, "要留下反向说明：报告由 Reporter 阶段产出，不要排报告步骤"


# --------------------------------------------------------------------------- #
# 二、检测：真被排出来就响亮拒绝（绝不执行、绝不标 skipped）
# --------------------------------------------------------------------------- #
def test_report_step_is_refused_loudly(monkeypatch):
    seen = _spy_execute_tool(monkeypatch)
    s = _state()
    s.plan = PlanModel(goal="g", steps=[_report_step()])
    run_executor(s)

    hits = [r for r in s.tool_results if r.tool == "generate_report"]
    assert hits, s.tool_results
    r = hits[0]
    assert r.status == "FAILED"
    assert r.skipped is False, (
        "排了个做不到的步骤 ≠ '没轮到'：必须留在工具成功率的**失败分母**里（E6/03）")
    assert "不能作为计划步骤" in (r.error or "")
    assert "Reporter" in (r.error or ""), "错误信息要让人知道正确做法"
    assert "generate_report" not in seen, "拒绝必须是**未执行**——一执行就会渲染那份空壳"


def test_guard_fires_before_the_dependency_cascade():
    """D54 里那 13 条假象就是被 `依赖步骤未完成` 盖住的——它**不是**依赖问题。

    所以守卫必须排在依赖检查**之前**，否则同一件事有两条互相矛盾的说法。
    """
    s = _state()
    s.plan = PlanModel(goal="g", steps=[_report_step(deps=["step_1"])])  # step_1 不存在
    run_executor(s)

    r = [x for x in s.tool_results if x.tool == "generate_report"][0]
    assert r.status == "FAILED"
    assert r.error != "依赖步骤未完成"
    assert "不能作为计划步骤" in r.error


def test_real_data_steps_in_the_same_plan_still_run(monkeypatch):
    """守卫只钉那一颗钉子：同一批里的真实步骤不受影响。"""
    seen = _spy_execute_tool(monkeypatch)
    s = _state()
    s.plan = PlanModel(goal="g", steps=[_sql_step(), _report_step(deps=["step_1"])])
    run_executor_all(s)

    assert "sql_query" in seen, "真实取数步骤必须照跑"
    assert "generate_report" not in seen
    assert not any(r.error == "依赖步骤未完成" for r in s.tool_results), (
        "报告步骤被拒后不得再冒出'依赖未完成'这条假话")


def test_the_forbidden_set_is_declared_exactly_once():
    """名单必须小到能一眼看完：扩名单要同时改这份用例（防悄悄扩大禁令）。"""
    from app.core.tools.specs import NOT_PLANNABLE_TOOLS

    assert NOT_PLANNABLE_TOOLS == frozenset({"generate_report"})


def test_every_forbidden_tool_is_blocked_on_both_paths():
    """名单里**每一个**名字都要在两条路上同时失效——预防/检测同源，不许漂移。"""
    from app.core.tools.specs import NOT_PLANNABLE_TOOLS

    for name in NOT_PLANNABLE_TOOLS:
        names, _ = select_tools_for_planner("任意问题", threshold_count=999)
        assert name not in names, f"{name} 仍对 planner 可见"
        s = _state()
        s.plan = PlanModel(goal="g", steps=[
            PlanStep(id="step_1", objective="o", action="a", tool=name)])
        run_executor(s)
        assert s.tool_results and s.tool_results[0].skipped is False, name


# --------------------------------------------------------------------------- #
# 三、交付物不变量：删掉这个入口**不会**让任何人拿不到报告
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_env(monkeypatch):
    """离线（MockLLM）跑完整图——报告由 Reporter 阶段真产出。"""
    from app.infrastructure.llm.router import reset_llm

    monkeypatch.setenv("MOCK_LLM", "true")
    # 关掉未启动的中间件：Redis 未起时每次操作要等 ~2s 连接超时（节点级 ×4s）
    for var in ("REDIS_URL", "POSTGRES_DSN", "MILVUS_HOST"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def test_deliverable_invariant_when_planner_emits_a_report_step(mock_env, monkeypatch):
    """**本卡最重要的一条**：计划里含报告步骤，用户照样拿到非空的真报告。

    这是 §2.1「从菜单里删掉一个工具」**安全**的前提；
    同时补上 D56 那次误判的根——"用户到底拿到了什么"由测试**直接钉住**，
    不能靠"我能看到步骤状态"去推断（`state.report` 不进 checkpoint）。
    """
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import MockLLM

    orig = MockLLM._stage_planner

    def _planner_that_misbehaves(self, user: str):
        plan = orig(self, user)
        plan["steps"].append({
            "id": "step_9", "objective": "出报告", "action": "汇总报告",
            "tool": "generate_report", "dependencies": ["step_3"],
            "expected_output": "报告", "success_criteria": "有报告",
        })
        return plan

    monkeypatch.setattr(MockLLM, "_stage_planner", _planner_that_misbehaves)

    state = run_analysis("e2_05_report_step", "分析各区域营收")

    assert any(s.tool == "generate_report" for s in state.plan.steps), (
        "计划里没这个步骤 → 本用例是空转的，钉不住任何东西")

    hits = [r for r in state.tool_results if r.tool == "generate_report"]
    assert hits and all(r.status == "FAILED" for r in hits), (
        "报告步骤不可能成功——它吃的是执行器阶段还不存在的 analysis")
    assert all(r.skipped is False for r in hits)

    assert state.status == "FINISH", f"实际 {state.status}，error={state.error}"
    assert state.report.strip(), "用户必须有报告可读"
    assert state.context.objective[:20] in state.report or len(state.report) > 300, (
        f"报告不能是 §1.2 那种 163 字符空壳（实际 {len(state.report)} 字符）")
    assert len(state.report) > 200, f"实际 {len(state.report)} 字符"


# --------------------------------------------------------------------------- #
# 四、别误删：工具本身还在（只是不许排进计划）
# --------------------------------------------------------------------------- #
def test_tool_is_still_registered_and_declared():
    from app.core.tools import REGISTRY

    assert "generate_report" in REGISTRY, "run_reporter 的模板兜底要用它"
    assert "generate_report" in TOOL_SPECS, "RBAC / MCP 的权限声明还引用它"


def test_tool_still_renders_when_called_directly():
    """守卫在**步骤级**：直接调用（`test_agent_real.py::test_generate_report_runs`、
    Reporter 的模板兜底）必须继续可用——本卡没动渲染链路。"""
    from app.core.tools.report_tool import run as report_run

    out = report_run({"analysis": {}, "reflection": None, "objective": "分析各区域营收"})
    assert out.get("report", "").strip()
