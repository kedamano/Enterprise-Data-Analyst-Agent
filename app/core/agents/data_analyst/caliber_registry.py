"""E4/05 口径注册表（CRUD）— D48。

Spec: docs/specs/E4/05-caliber-registry.md §1

登记指标的**基准口径**（人工录入，非 LLM 产出），供 ``caliber_check`` 比对
"报告里写的口径"与"应该是什么口径"。是**参照数据**，不是 append-only 审计流——
所以用 JSON 文件 + 原子写，而非 JSONL（JSONL 不适合 CRUD 覆盖/删除）。

后端：默认 JSON 文件 ``data/caliber_registry.json``；
``CALIBER_REGISTRY_DB_URL`` 留作 sqlite 扩展位（本卡只实现文件后端，宁缺勿滥）。
**不做双写**（沿用 D46 审计落库纪律）。

读故障绝不打断 ``caliber_check``（见 caliber.py 的 try/except）；
CRUD 写故障对调用方抛（显式管理动作不该吞）。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from ...safe_fs import purge_file

from pydantic import BaseModel, Field

logger = logging.getLogger("da.caliber")

_DEFAULT_PATH = Path("data/caliber_registry.json")


class CaliberSpec(BaseModel):
    """一条指标的登记基准口径（人工录入）。"""

    metric: str
    filters: list[str] = Field(default_factory=list)
    unit: str = ""
    period_type: str = ""
    denominator: str = ""
    grain: str = ""
    notes: str = ""


class CaliberRegistry:
    """JSON 文件后端的口径注册表（CRUD）。按 ``metric`` 主键幂等。"""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else _DEFAULT_PATH

    # -- 读 --------------------------------------------------------------- #
    def list_all(self) -> list[CaliberSpec]:
        data = self._load()
        return [CaliberSpec(**d) for d in data]

    def get(self, metric: str) -> Optional[CaliberSpec]:
        if not metric:
            return None
        for d in self._load():
            if d.get("metric") == metric:
                return CaliberSpec(**d)
        return None

    # -- 写（对调用方抛） ------------------------------------------------- #
    def register(self, spec: CaliberSpec) -> None:
        if not spec.metric:
            raise ValueError("CaliberSpec.metric 不可为空")
        data = self._load()
        data = [d for d in data if d.get("metric") != spec.metric]
        data.append(spec.model_dump())
        self._save(data)

    def remove(self, metric: str) -> None:
        data = self._load()
        data = [d for d in data if d.get("metric") != metric]
        self._save(data)

    # -- 内部 ------------------------------------------------------------- #
    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                return []
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            logger.warning("口径注册表读取失败（%s）", self.path, exc_info=True)
            return []

    def _save(self, data: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再 rename（防中途崩溃留下半截 JSON）
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   suffix=".tmp", prefix=".caliber_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            # 物理清理临时文件：走 safe_fs，避免环境安全删除钩子的 SystemExit
            # 顶替掉真正的写入异常（那样调用方会看到"进程要退出"而不是写失败原因）。
            purge_file(tmp)
            raise


_default: Optional[CaliberRegistry] = None


def _default_registry() -> CaliberRegistry:
    """全局默认实例（``caliber_check`` 读它做 caliber_deviation 比对）。

    测试用 monkeypatch 替换本函数返回值来注入临时注册表。
    """
    global _default
    if _default is None:
        _default = CaliberRegistry()
    return _default
