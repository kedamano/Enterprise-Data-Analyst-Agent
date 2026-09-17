"""#6 eval 成本折算：**"单价未知"与"已知免费"必须可区分**。

修复前的真缺陷：`cost_input_per_mtok` / `cost_output_per_mtok` 默认 `0.0`，
而 runner 的判据是 `if pi > 0 or po > 0` —— 于是

- 没配单价 → `0.0` → 判为"未知" → 报 `None`；
- **免费模型（真实成本 0）→ 也是 `0.0` → 同样报 `None`**。

两件事用同一个值兼表，结果 `docs/progress/eval-real-baseline.md` 的成本列**恒为 None**，
而它跑的恰恰就是 `-free` 模型——"确实不花钱"被记成了"不知道多少钱"。

修法：默认改为 `None`（未配置 = 未知），显式 `0` 表示已知免费。
"""
from __future__ import annotations

from app.config import Settings, get_settings
from app.eval.runner import compute_cost_usd


# --------------------------------------------------------------------------- #
# 一、纯函数折算
# --------------------------------------------------------------------------- #
def test_unknown_price_yields_none():
    """单价未配置（None）→ 成本未知。"""
    assert compute_cost_usd(None, None, 1_000_000, 1_000_000) is None


def test_free_model_yields_zero_not_none():
    """显式 0（免费档）→ 0.0，**不能**退化成 None。"""
    assert compute_cost_usd(0.0, 0.0, 124_368, 30_534) == 0.0


def test_paid_model_computes_usd():
    # 1M prompt @ $3 + 0.5M completion @ $15 = 3 + 7.5 = 10.5
    assert compute_cost_usd(3.0, 15.0, 1_000_000, 500_000) == 10.5


def test_one_sided_price_treats_missing_side_as_zero():
    """只配了输入价 → 输出按 0 折算（而不是整条判为未知）。"""
    assert compute_cost_usd(2.0, None, 1_000_000, 999_999) == 2.0


def test_no_tokens_yields_none():
    """没有 token 上报时无从折算——即便配了单价也是 None。"""
    assert compute_cost_usd(3.0, 15.0, 0, 0) is None


# --------------------------------------------------------------------------- #
# 二、配置默认值：未设置 ≠ 0
# --------------------------------------------------------------------------- #
def test_settings_default_is_none_not_zero():
    """默认必须是 None——否则"没配单价"会被当成"已知免费"，成本列永远是 0.0。"""
    s = Settings(_env_file=None)
    assert s.cost_input_per_mtok is None
    assert s.cost_output_per_mtok is None


def test_settings_explicit_zero_is_kept(monkeypatch):
    """显式 0 必须原样保留（不能因为是 0 就被当成"没配"）。"""
    monkeypatch.setenv("COST_INPUT_PER_MTOK", "0")
    monkeypatch.setenv("COST_OUTPUT_PER_MTOK", "0")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.cost_input_per_mtok == 0.0
        assert s.cost_output_per_mtok == 0.0
        assert compute_cost_usd(s.cost_input_per_mtok, s.cost_output_per_mtok, 1000, 1000) == 0.0
    finally:
        get_settings.cache_clear()
