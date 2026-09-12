"""真实模型专属的健壮性修复（真实 e2e 实测暴露）。

1. **image_analyze 参数别名**：模型按自己的习惯写 ``image_path``（而非文档里的
   ``image``），旧实现直接判「缺少参数 image」→ 整个视觉步骤 FAILED、连带
   generate_report 因依赖未完成而 FAILED。真实 e2e 上就是这么挂的。
2. **畸形 SQL 修复**：模型把 ``SUM("revenue")`` 写成 ``SUM("revenue"))``，
   而 ``_qualify_upload`` 只会追加括号 → 语法错误。现在先做**最小修复**（删多余
   闭括号/补缺失闭括号）保留模型的聚合意图，修不好才退回合成 SQL。
3. **参数完全缺失的自然语言兜底**：真实 gemma 的 ``input`` 是 ``null``，参数被
   写进了 objective 人话（"...values of sales_chart.png"）。此时任何别名表都没用，
   必须从文本里"捞"文件名。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.agents.data_analyst.nodes import (
    _is_numeric_col,
    _repair_sql_balance,
    _sql_balanced,
)
from app.core.tools import vision_tool


# --------------------------------------------------------------------------- #
# _sql_balanced
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "SELECT SUM(x) FROM t",
    "SELECT * FROM t WHERE a = 'x'",
    'SELECT "region", SUM("revenue") AS r FROM upload.sales GROUP BY "region"',
    "SELECT * FROM t WHERE a IN ('a', 'b')",
    "SELECT (1 + 2) * 3",
    "",
    None,
])
def test_sql_balanced_accepts_valid(sql):
    if not sql:
        assert not _sql_balanced(sql or "")
    else:
        assert _sql_balanced(sql)


@pytest.mark.parametrize("sql", [
    'SELECT SUM("revenue")) FROM t',          # 多一个右括号（真实 gemma 产出的形态）
    'SELECT * FROM t WHERE a IN ("a","b"',    # 少一个右括号
    "SELECT * FROM t WHERE a = 'unclosed",    # 引号未闭合
    ") SELECT * FROM t",
])
def test_sql_balanced_rejects_malformed(sql):
    assert not _sql_balanced(sql)


# --------------------------------------------------------------------------- #
# _repair_sql_balance —— 保留模型正确意图，而不是简单丢弃
# --------------------------------------------------------------------------- #
def test_repair_removes_extra_closing_paren():
    """真实 gemma 形态：多一个 ) → 修好后仍是 SUM 聚合（而不是退化成 COUNT）。"""
    bad = 'SELECT "region", SUM("revenue")) AS total_revenue FROM upload.sales GROUP BY "region"'
    fixed = _repair_sql_balance(bad)
    assert _sql_balanced(fixed)
    assert "SUM" in fixed
    assert fixed.count(")") == fixed.count("(")
    assert fixed == ('SELECT "region", SUM("revenue") AS total_revenue '
                     'FROM upload.sales GROUP BY "region"')


def test_repair_appends_missing_closing_paren():
    fixed = _repair_sql_balance('SELECT * FROM t WHERE a IN ("a","b"')
    assert _sql_balanced(fixed)
    assert fixed.endswith('")')


def test_repair_leaves_valid_sql_untouched():
    good = "SELECT SUM(x) FROM t WHERE a = 'b'"
    assert _repair_sql_balance(good) == good


def test_repair_gives_up_on_unbalanced_quotes():
    """引号都不配平时不做危险修复，原样返回（调用方按不合法处理）。"""
    bad = "SELECT * FROM t WHERE a = 'unclosed"
    assert _repair_sql_balance(bad) == bad


# --------------------------------------------------------------------------- #
# _is_numeric_col —— 合成 SQL 兜底时优先 SUM 而不是 COUNT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("col,expected", [
    ("revenue", True),
    ("units", True),
    ("total_amount", True),
    ("营收", True),
    ("region", False),
    ("month", False),
    ("product", False),
])
def test_is_numeric_col(col, expected):
    assert _is_numeric_col(col) is expected


# --------------------------------------------------------------------------- #
# image_analyze 参数别名
# --------------------------------------------------------------------------- #
def test_first_param_accepts_aliases():
    assert vision_tool._first_param({"image_path": "a.png"}, "image", "image_path") == "a.png"
    assert vision_tool._first_param({"path": "b.png"}, "image", "path") == "b.png"
    assert vision_tool._first_param({"image": "c.png"}, "image") == "c.png"
    assert vision_tool._first_param({"image": "  "}, "image") == ""


def test_first_param_nested_dict():
    assert vision_tool._first_param(
        {"image": {"path": "nested.png"}}, "image"
    ) == "nested.png"


def test_first_param_prefers_first_nonempty():
    got = vision_tool._first_param({"image": "", "image_path": "x.png"}, "image", "image_path")
    assert got == "x.png"


# --------------------------------------------------------------------------- #
# 自然语言兜底：input=null 时从 objective 里捞文件名
# --------------------------------------------------------------------------- #
def test_guess_image_from_objective_sentence():
    """真实 gemma 的 objective 原话，input 为 null。"""
    got = vision_tool._guess_image_from_text(
        "Read and describe the content and key values of sales_chart.png"
    )
    assert got == "sales_chart.png"


@pytest.mark.parametrize("text,expected", [
    ("分析 sales_chart.png 中的趋势", "sales_chart.png"),
    ("look at `revenue_2024.jpg` please", "revenue_2024.jpg"),
    ("识别 dashboard.webp 的指标", "dashboard.webp"),
    ("这张图 IMG_001.JPEG 说明了什么", "IMG_001.JPEG"),
])
def test_guess_image_from_text_variants(text, expected):
    assert vision_tool._guess_image_from_text(text) == expected


def test_guess_image_from_text_returns_empty_when_absent():
    assert vision_tool._guess_image_from_text("分析一下营收情况", "") == ""


def test_run_recovers_image_from_objective(monkeypatch, tmp_path):
    """端到端：模型没给 image，只在 objective 里写了文件名 —— 也必须能跑通。"""
    img = tmp_path / "sales_chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    monkeypatch.setattr(vision_tool, "_resolve_image_path", lambda image, sid: str(img))

    class FakeLLM:
        def vision(self, system, user, image_paths, **kw):
            return '{"summary": "bar chart of revenue by region"}'

    import app.infrastructure.llm.router as R
    monkeypatch.setattr(R, "get_llm", lambda: FakeLLM())

    out = vision_tool.run(
        {"_objective": "Read ... sales_chart.png", "question": "读图"},
        workdir=str(tmp_path),
    )
    assert out.get("ok") is True, out
    assert out["image"] == "sales_chart.png"


def test_run_accepts_image_path_alias(monkeypatch, tmp_path):
    """真实模型的写法（image_path）必须能被接受，而不是判「缺少参数」。"""
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    monkeypatch.setattr(vision_tool, "_resolve_image_path", lambda image, sid: str(img))

    class FakeLLM:
        def vision(self, system, user, image_paths, **kw):
            return '{"summary": "chart shows 4 regions", "values": {"West": 120}}'

    import app.infrastructure.llm.router as R
    monkeypatch.setattr(R, "get_llm", lambda: FakeLLM())

    # 旧实现会在这里返回「缺少参数 image」
    out = vision_tool.run({"image_path": str(img), "question": "读图"},
                          workdir=str(tmp_path))
    assert out.get("ok") is True, out
    assert out["image"]  # 回填了实际使用的图片标识


def test_run_still_reports_missing_when_truly_absent(tmp_path):
    out = vision_tool.run({}, workdir=str(tmp_path))
    assert out["ok"] is False
    assert "缺少参数 image" in out["error"]
