"""E4/05 口径注册表 CRUD 端点 — D48。

Spec: docs/specs/E4/05-caliber-registry.md §3

登记/查询/删除指标的基准口径，供 ``caliber_check`` 比对"报告口径"
与"应该是什么口径"。CRUD 是显式管理动作 → 写故障对调用方抛（不吞）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.agents.data_analyst.caliber_registry import CaliberSpec, _default_registry

router = APIRouter(prefix="/chat", tags=["caliber"])


class CaliberBody(BaseModel):
    metric: str = Field(..., min_length=1)
    filters: list[str] = Field(default_factory=list)
    unit: str = ""
    period_type: str = ""
    denominator: str = ""
    grain: str = ""
    notes: str = ""


def _registry():
    # 走默认实例（与 caliber_check 同口径）——测试 monkeypatch _default_registry 即可隔离
    return _default_registry()


@router.get("/analyze/caliber")
def list_calibers(metric: str | None = Query(default=None)):
    """列出全部登记口径；带 ``metric`` 则取单条（无则 404）。"""
    if metric:
        spec = _registry().get(metric)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"未登记口径: {metric}")
        return spec.model_dump()
    return [s.model_dump() for s in _registry().list_all()]


@router.post("/analyze/caliber", status_code=201)
def register_caliber(body: CaliberBody):
    """登记/覆盖一条口径（按 metric 主键幂等）。"""
    _registry().register(CaliberSpec(**body.model_dump()))
    return {"metric": body.metric, "ok": True}


@router.delete("/analyze/caliber")
def remove_caliber(metric: str = Query(...)):
    """删除一条口径（不存在也 200）。"""
    _registry().remove(metric)
    return {"metric": metric, "ok": True}
