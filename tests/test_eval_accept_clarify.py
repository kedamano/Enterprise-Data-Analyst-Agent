"""E6/01 §2.6 `accept_clarify`：**判断型问题**的反问是正确行为，不该被判失败。

证据（真实基线，两个不同模型一致）
----------------------------------
`r_ratio_denominator`（"转化率 6%，环比提升 7%，显著吗？"）、
`r_causal_overreach`（"渠道切换是营收下降的原因吗？"）、
`r_simpson_check`（"总转化率 6%→7%，说明优化成功吗？"）
—— 这三条在 **glm-5.3** 与 **deepseek-v4-flash** 上都稳定返回 `CLARIFY`。

换模型行为不变 ⇒ 不是某个模型的怪癖，而是**系统性的语义不匹配**：
这类题把数字**给在题面里**，要的是**统计判断**，不需要从库里取数；
而澄清策略遇到"库里没有对应数据"就倾向反问。**模型做得对，是 golden 的期望不对。**

设计要点（防止把"没做分析"混进"做对了"）
----------------------------------------
- `accept_clarify=True` 时，CLARIFY 记为独立终态 **`CLARIFY_OK`**，
  **不进 FINISH 率**（FINISH 率只认真正的 FINISH）；
- CLARIFY 下**没有分析可断言** → 跳过内容类断言（否则 must_find / 门禁 code 都会假红）；
- 计数 `clarify_accepted` 单独报出，读者一眼能看出"通过的里有几条其实只是反问"。
"""
from __future__ import annotations

import inspect

from app.eval.golden import GOLDEN, GoldenCase
from app.eval.runner import resolve_terminal

JUDGMENT_CASES = ("r_ratio_denominator", "r_causal_overreach", "r_simpson_check")


def _case(case_id: str) -> GoldenCase:
    hit = [c for c in GOLDEN if c.id == case_id]
    assert hit, f"golden 里找不到 {case_id}"
    return hit[0]


# --------------------------------------------------------------------------- #
# 一、golden 标记
# --------------------------------------------------------------------------- #
def test_judgment_cases_accept_clarify():
    for cid in JUDGMENT_CASES:
        assert _case(cid).accept_clarify, f"{cid} 是判断型问题，应允许反问"


def test_other_cases_do_not_accept_clarify():
    """不能因为"允许反问"变成普遍后门——取数型用例反问仍应判失败。"""
    for cid in ("r_join_amplification_guard", "r_decompose_before_attribution",
                "r_multiple_comparison", "r_caliber_period_mismatch"):
        assert not _case(cid).accept_clarify, f"{cid} 反问应判失败"


def test_default_is_false():
    assert GoldenCase(id="t", query="q").accept_clarify is False


# --------------------------------------------------------------------------- #
# 二、终态归一（纯函数）
# --------------------------------------------------------------------------- #
def test_clarify_becomes_clarify_ok_when_accepted():
    case = GoldenCase(id="t", query="q", accept_clarify=True)
    status, failures = resolve_terminal(case, "CLARIFY", None)
    assert status == "CLARIFY_OK"
    assert failures == [], "被接受的澄清不该产生失败断言"


def test_clarify_still_fails_when_not_accepted():
    case = GoldenCase(id="t", query="q")
    status, failures = resolve_terminal(case, "CLARIFY", None)
    assert status == "CLARIFY"
    assert failures and "期望 FINISH" in failures[0]


def test_finish_passes_through():
    case = GoldenCase(id="t", query="q")
    assert resolve_terminal(case, "FINISH", None) == ("FINISH", [])


def test_error_status_still_fails():
    case = GoldenCase(id="t", query="q", accept_clarify=True)
    status, failures = resolve_terminal(case, "ERROR", "boom")
    assert status == "ERROR"
    assert failures, "非 CLARIFY 的异常终态不得被 accept_clarify 放过"


def test_clarify_ok_is_not_counted_as_finish():
    """**关键**：CLARIFY_OK 不是 FINISH——FINISH 率不得被反问虚高。"""
    assert resolve_terminal(GoldenCase(id="t", query="q", accept_clarify=True),
                            "CLARIFY", None)[0] != "FINISH"


# --------------------------------------------------------------------------- #
# 三、接线：CLARIFY_OK 必须**跳过内容断言**（无分析可断言）
# --------------------------------------------------------------------------- #
def test_evaluate_case_skips_content_assertions_on_clarify_ok():
    """反问时 report/findings 都是空的，内容断言必然假红 → 必须早退。"""
    src = inspect.getsource(__import__("app.eval.runner", fromlist=["x"]).evaluate_case)
    assert "CLARIFY_OK" in src
    # 早退必须发生在内容断言之前
    assert src.index("CLARIFY_OK") < src.index("must_find")
