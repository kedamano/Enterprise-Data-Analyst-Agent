"""``data/sample_analyst.db`` 演示数据集：**陷阱必须真的落在库里**。

背景（详见 ``docs/progress/pending-real.md`` §C.1）：`app/eval/golden.py` 里 7 条
``requires_real=True`` 用例是按一份业务数据集写的，而内置 ``sample_enterprise.db``
只有 ``fact_sales + 3 张维表``（字段仅 revenue/orders/customers）。
真 LLM 重跑时 5 题因**字段不存在**而 CLARIFY、2 题因无数据而使质量探测器不触发。

``scripts/generate_analyst_sample.py`` 造一个**超集**库补齐这些实体，并**刻意植入可判定的陷阱**。
本文件守两件事：

1. **陷阱真的有**（且是从**库内数据**算出来的，不是断言生成器的内部变量）；
2. **工具真的能在这个库上跑通**（`sql_query` / `dataset_profile` 不因新 schema 报错）。

数据集是确定性的（固定种子），所以这些断言在任何机器上都稳定。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import pytest
from app.config import get_settings
from scripts import generate_analyst_sample as gen


@pytest.fixture(scope="module")
def analyst_db(tmp_path_factory) -> Path:
    """确定性生成到临时目录（不写进仓库）。"""
    db = tmp_path_factory.mktemp("analyst") / "sample_analyst.db"
    gen.main(str(db))
    return db