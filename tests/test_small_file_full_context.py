"""P2-1 小文件全文进上下文：命中阈值（<50KB 且 <2000 行）的表格附件，
必须把**整张表**直接渲染进 prompt，而非只给样例。

价值：小数据集无需 SQL 往返即可被模型就地计算，分析质量与延迟同时受益。
锁死行为：阈值边界、CSV 转义、JSON 同样生效。
"""
from __future__ import annotations

from app.core.attachments import (
    SMALL_FILE_MAX_BYTES,
    SMALL_FILE_MAX_ROWS,
    DatasetPreview,
    build_preview,
)

# 一个小 CSV：6 行，远低于阈值
SMALL_CSV = b"""person_id,gender,occupation,sleep_quality,stress
1,1,Office Worker,6,7
2,2,Student,7,4
3,1,Retired,8,3
4,2,Office Worker,5,8
5,1,Student,6.5,6
6,2,Office Worker,4,9
"""

SMALL_JSON = b"""[
  {"name": "Alice", "score": 90, "note": "good"},
  {"name": "Bob", "score": 75, "note": "ok"},
  {"name": "Cara", "score": 88, "note": "fine"}
]
"""


# --------------------------------------------------------------------------- #
# 注入行为
# --------------------------------------------------------------------------- #
def test_small_csv_injects_full_data():
    p = build_preview(SMALL_CSV, "small.csv")
    assert p.is_small is True
    ctx = p.to_context()
    # 必须出现全文块，且包含样例之外的完整数据
    assert "全文数据" in ctx
    assert "```csv" in ctx
    # 6 行全部在上下文中（样例只截 5 行，但全文应含最后一行 Office Worker/4/9）
    assert "Office Worker,4,9" in ctx
    assert "Student,6.5,6" in ctx


def test_small_json_also_injects_full_data():
    """JSON 解析的附件同样要填充 all_rows 并渲染全文。"""
    p = build_preview(SMALL_JSON, "small.json")
    assert p.is_small is True
    assert p.all_rows, "JSON 解析必须填充 all_rows，否则 P2-1 对 JSON 失效"
    ctx = p.to_context()
    assert "全文数据" in ctx
    # 第三条记录（Cara）应出现在全文里
    assert "Cara" in ctx and "88" in ctx


def test_large_row_count_not_injected():
    """行数越过阈值 —— 即使是小字节也不注入全文。"""
    p = DatasetPreview(
        name="big.csv",
        kind="table",
        rows=SMALL_FILE_MAX_ROWS + 1,  # 2001
        columns=["a", "b"],
        sample=[{"a": 1, "b": 2}],
        all_rows=[{"a": i, "b": i} for i in range(SMALL_FILE_MAX_ROWS + 1)],
        bytes=5_000,  # 字节很小
    )
    assert p.is_small is False
    ctx = p.to_context()
    assert "全文数据" not in ctx


def test_large_bytes_not_injected():
    """字节越过阈值 —— 即便行数少也不注入全文（宽表保护）。"""
    p = DatasetPreview(
        name="wide.csv",
        kind="table",
        rows=10,
        columns=["a", "b"],
        sample=[{"a": 1, "b": 2}],
        all_rows=[{"a": 1, "b": 2}],
        bytes=SMALL_FILE_MAX_BYTES + 1,  # 50001
    )
    assert p.is_small is False
    assert "全文数据" not in p.to_context()


def test_is_small_boundary_exact():
    """阈值边界必须精确：含端点为小，越端点为非小。"""
    assert DatasetPreview(name="x", kind="table", rows=SMALL_FILE_MAX_ROWS,
                          columns=["a"], bytes=SMALL_FILE_MAX_BYTES).is_small is True
    assert DatasetPreview(name="x", kind="table", rows=SMALL_FILE_MAX_ROWS + 1,
                          columns=["a"], bytes=SMALL_FILE_MAX_BYTES).is_small is False
    assert DatasetPreview(name="x", kind="table", rows=10,
                          columns=["a"], bytes=SMALL_FILE_MAX_BYTES + 1).is_small is False
    # 空表不算小
    assert DatasetPreview(name="x", kind="table", rows=0,
                          columns=["a"], bytes=10).is_small is False


def test_full_data_csv_escapes_special_chars():
    """含逗号/引号的单元格必须正确转义，否则会破坏 CSV 结构。"""
    p = DatasetPreview(
        name="notes.csv",
        kind="table",
        rows=1,
        columns=["id", "comment"],
        sample=[{"id": 1, "comment": 'says "hi", then left'}],
        all_rows=[{"id": 1, "comment": 'says "hi", then left'}],
        bytes=100,
    )
    block = p._render_full_data()
    assert '"says ""hi"", then left"' in block


def test_full_data_truncates_beyond_cap():
    """极端情况下全文超长应截断，不撑爆 prompt。"""
    from app.core.attachments import _FULL_DATA_CONTEXT_CAP

    big = [{"id": i, "v": i} for i in range(5000)]
    p = DatasetPreview(
        name="huge.csv", kind="table", rows=5000, columns=["id", "v"],
        sample=big[:1], all_rows=big, bytes=10_000,
    )
    block = p._render_full_data()
    assert len(block) <= _FULL_DATA_CONTEXT_CAP + 100
