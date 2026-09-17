"""D38：`must_find` 会被**回显的问题**满足 —— 又一类假绿。

实测（`r_join_amplification_guard`，真实基线）
-------------------------------------------
该用例断言 `must_find=("品类",)`，判为 ✅。但查报告正文：

```
## 各品类营收统计报告          ← 标题里就有"品类"
**目标：** 统计各品类营收。     ← 回显了用户问题
**状态：** 无法完成。当前分析过程中，未…   ← 正文其实说"做不了"
```

`must_find` 是**子串命中**，而报告天然会回显问题（标题/目标段），于是
**只要问题里出现过该词，断言就恒真**——哪怕分析产出了 0 条 findings、
正文明确写"无法完成"。

这与 `r_join_amplification_guard` 原本"惩罚正确行为"是同一族的病：
**断言测的不是它想测的东西**。

修法
----
`GoldenCase.min_findings`：要求本轮**真的产出**若干 findings。
业务内容类用例设为 1 —— "答出各品类营收"的前提是**至少有发现**，
回显问题不算。`accept_clarify` 的用例（反问、本就没分析）不设。
"""
from __future__ import annotations

from app.core.agents.data_analyst.state import AnalysisResult
from app.eval.golden import GOLDEN, GoldenCase
from app.eval.runner import findings_violations

# 这些用例断言"答出业务内容" → 前提是真的产出了发现
BUSINESS_CASES = ("r_join_amplification_guard", "r_decompose_before_attribution",
                  "r_causal_overreach", "r_multiple_comparison",
                  "r_caliber_period_mismatch")


def _case(cid: str) -> GoldenCase:
    hit = [c for c in GOLDEN if c.id == cid]
    assert hit, f"golden 里找不到 {cid}"
    return hit[0]


# --------------------------------------------------------------------------- #
# 一、契约
# --------------------------------------------------------------------------- #
def test_default_is_zero():
    assert GoldenCase(id="t", query="q").min_findings == 0


def test_business_cases_require_findings():
    for cid in BUSINESS_CASES:
        assert _case(cid).min_findings >= 1, f"{cid} 断言业务内容，应要求真的产出发现"


def test_clarify_cases_do_not_require_findings():
    """反问型用例本就没做分析，不能要求 findings（否则与 accept_clarify 打架）。"""
    for cid in ("r_ratio_denominator", "r_simpson_check"):
        assert _case(cid).min_findings == 0, cid


# --------------------------------------------------------------------------- #
# 二、判定（纯函数）
# --------------------------------------------------------------------------- #
def test_insufficient_findings_is_reported():
    case = GoldenCase(id="t", query="q", min_findings=1)
    problems = findings_violations(case, 0)
    assert problems and "发现" in problems[0]


def test_sufficient_findings_pass():
    case = GoldenCase(id="t", query="q", min_findings=1)
    assert findings_violations(case, 1) == []
    assert findings_violations(case, 3) == []


def test_zero_requirement_always_passes():
    case = GoldenCase(id="t", query="q")
    assert findings_violations(case, 0) == []


# --------------------------------------------------------------------------- #
# 三、这正是那条假绿的成因（回归钉死）
# --------------------------------------------------------------------------- #
def test_empty_analysis_would_now_fail_the_join_case():
    """复现现场：0 findings + 报告回显了问题 → 旧断言给 ✅，新断言必须给 ❌。"""
    case = _case("r_join_amplification_guard")
    empty = AnalysisResult()          # findings 为空，正对真实基线的情况
    assert not empty.findings
    assert findings_violations(case, 0), "0 findings 时该用例必须判失败（回显不算答出）"
