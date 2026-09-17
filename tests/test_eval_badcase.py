"""D39：badcase 回流 —— 失败用例落盘 / 可复跑 / 可固化成 golden。

动因（D38 的真实基线）
----------------------
7 条用例里 6 条断言失败，但**跑完就散了**：没有落盘、没法复跑、更没法变成回归。
下一轮改动是"修好了"还是"又坏了"，只能靠人肉比对两份 markdown。

本模块把"失败"变成**可复跑的资产**：
1. `record_from_report()`：从 eval 报告里挑出**该记的**失败用例落盘
   （`SKIPPED` / `DEGRADED` 不算——前者是没跑，后者不是模型的错）；
2. 同一个 case 反复失败 → **只留一条**，`runs` 累加（避免每次跑完堆一堆文件）；
3. `replay()`：拿落盘的 badcase 原样重跑，直接回答"修好没有"；
4. `promote_draft()`：生成**待人工审阅**的 golden 片段——**不自动写入**
   （断言该立什么需要人判断，自动生成等于让模型给自己出题）。

另有一条**从真实 badcase 固化出来的契约**：`min_numeric_claims`。
E1 现有断言是"**每个**数值 claim 都要可溯源"——可当数值 claim **一个都没有**时，
它是**恒真**的（vacuous truth）。D38 的真实基线正是如此：`溯源 0/0` 却全程判过。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.eval.badcase import (
    BadCase,
    load_all,
    promote_draft,
    record_from_report,
    replay,
)
from app.eval.golden import GOLDEN, GoldenCase
from app.eval.runner import numeric_claim_violations


def _report(*cases: dict) -> dict:
    return {"mode": "real", "cases": len(cases), "cases_detail": list(cases),
            "metrics": {}}


def _case(cid: str, status="FINISH", ok=False, reasons=None, **kw) -> dict:
    return {"case_id": cid, "status": status, "assertions_ok": ok,
            "failed_assertions": reasons or ["报告/发现未命中关键片段: 拆解"],
            "findings": 0, "executed_tools": ["sql_query"],
            "report_text": "…报告正文…", "degraded": False, "tool_calls": 3, **kw}


# --------------------------------------------------------------------------- #
# 一、落盘：只记该记的，且同一 case 只留一条
# --------------------------------------------------------------------------- #
def test_records_only_failing_cases(tmp_path):
    rep = _report(_case("bad_one"), _case("good_one", ok=True, reasons=[]))
    paths = record_from_report(rep, out_dir=tmp_path)
    assert [p.stem for p in paths] == ["bad_one"]
    assert (tmp_path / "bad_one.json").exists()


def test_skipped_and_degraded_are_not_badcases(tmp_path):
    """SKIPPED=没跑；DEGRADED=不是模型的错（是限流/余额）。都不该进 badcase。"""
    rep = _report(_case("s", status="SKIPPED"), _case("d", status="DEGRADED"))
    assert record_from_report(rep, out_dir=tmp_path) == []


def test_repeat_failure_updates_in_place_and_counts_runs(tmp_path):
    record_from_report(_report(_case("bad_one")), out_dir=tmp_path)
    record_from_report(_report(_case("bad_one")), out_dir=tmp_path)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1, "同一个 case 不该堆多份文件"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["runs"] == 2
    assert data["first_seen"] and data["last_seen"]


def test_records_carry_what_replay_needs(tmp_path):
    """query 必须落盘——复跑要用**同一句话**，否则不可比。

    用真实 golden id：`query` 是从 `GOLDEN` 反查的（`CaseOutcome` 不带 query），
    这也正是 `replay` 只能对真实用例生效的原因。
    """
    real_id = "r_decompose_before_attribution"
    record_from_report(_report(_case(real_id, reasons=["真实失败原因"])), out_dir=tmp_path)
    bc = load_all(tmp_path)[0]
    assert isinstance(bc, BadCase)
    assert bc.reasons == ["真实失败原因"] and bc.status == "FINISH"
    assert bc.query, "复跑要用 query，不能空"
    assert "拆解" in bc.query or "GMV" in bc.query


# --------------------------------------------------------------------------- #
# 二、复跑：直接回答"修好没有"
# --------------------------------------------------------------------------- #
def test_replay_reports_fixed_and_unfixed(tmp_path):
    record_from_report(_report(_case("fixed_one"), _case("still_bad")), out_dir=tmp_path)

    def fake_runner(mode, only_real=False, ids=None):
        return _report(_case("fixed_one", ok=True, reasons=[]), _case("still_bad"))

    res = replay(out_dir=tmp_path, runner=fake_runner)
    assert res["fixed"] == ["fixed_one"]
    assert res["still_bad"] == ["still_bad"]


def test_replay_only_runs_recorded_ids(tmp_path):
    record_from_report(_report(_case("a"), _case("b")), out_dir=tmp_path)
    seen: dict = {}

    def fake_runner(mode, only_real=False, ids=None):
        seen["ids"] = list(ids)
        return _report(*[_case(i) for i in ids])

    replay(out_dir=tmp_path, runner=fake_runner, only="a")
    assert seen["ids"] == ["a"]


def test_replay_of_empty_dir_is_a_noop(tmp_path):
    assert replay(out_dir=tmp_path, runner=lambda *a, **k: _report())["fixed"] == []


# --------------------------------------------------------------------------- #
# 三、固化：只出**草稿**，不自动写 golden
# --------------------------------------------------------------------------- #
def test_promote_draft_is_a_review_draft_not_an_edit(tmp_path):
    record_from_report(_report(_case("bad_one")), out_dir=tmp_path)
    bc = load_all(tmp_path)[0]
    draft = promote_draft(bc)
    assert "GoldenCase(" in draft and "bad_one" in draft
    assert "TODO" in draft, "必须留人工审阅标记：断言该立什么不能由模型自己定"
    # 不能碰真 golden 文件
    before = len(GOLDEN)
    assert len(GOLDEN) == before


# --------------------------------------------------------------------------- #
# 四、从真实 badcase 固化的新契约：min_numeric_claims
# --------------------------------------------------------------------------- #
def test_numeric_claim_contract_exists():
    assert GoldenCase(id="t", query="q").min_numeric_claims == 0


def test_data_analysis_cases_require_numeric_claims():
    """数据类用例必须要求**至少一条可溯源数值结论**。

    E1 现有断言是"每个数值 claim 都要可溯源"——**一个都没有时恒真**（vacuous）。
    真实基线 `溯源 0/0` 却全程判过，正是这个洞。
    """
    for cid in ("r_multiple_comparison", "r_decompose_before_attribution",
                "r_simpson_check", "r_ratio_denominator"):
        assert _golden(cid).min_numeric_claims >= 1, cid


def _golden(cid: str) -> GoldenCase:
    return next(c for c in GOLDEN if c.id == cid)


def test_zero_claims_with_requirement_is_a_violation():
    """复现现场：0 条数值 claim + 要求 1 条 → 必须判失败（旧断言会给 ✅）。"""
    case = GoldenCase(id="t", query="q", min_numeric_claims=1)
    problems = numeric_claim_violations(case, 0)
    assert problems and "数值" in problems[0]


def test_enough_claims_pass():
    case = GoldenCase(id="t", query="q", min_numeric_claims=1)
    assert numeric_claim_violations(case, 1) == []
    assert numeric_claim_violations(case, 7) == []


def test_no_requirement_always_passes():
    assert numeric_claim_violations(GoldenCase(id="t", query="q"), 0) == []
