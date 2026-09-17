"""E4/05 口径注册表（CRUD）— D48。

Spec: docs/specs/E4/05-caliber-registry.md §1, §5

注册表是小而少写的**参照数据**（非 append-only 审计流）。
默认 JSON 文件后端 + 原子写；按 ``metric`` 主键幂等（同名覆盖）；
读故障绝不打断 ``caliber_check``（与 gate/caliber 既有纪律一致）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.agents.data_analyst.caliber_registry import (
    CaliberSpec,
    CaliberRegistry,
)


@pytest.fixture()
def registry(tmp_path: Path) -> CaliberRegistry:
    return CaliberRegistry(path=tmp_path / "caliber_registry.json")


def _spec(metric: str = "营收", **kw) -> CaliberSpec:
    base = dict(metric=metric, filters=["不含退货"], unit="万元",
                period_type="月", denominator="", grain="品类",
                notes="含税、按下单时间")
    base.update(kw)
    return CaliberSpec(**base)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_register_then_get(registry: CaliberRegistry):
    """注册一条 → get 命中、list 含之。"""
    spec = _spec("营收")
    registry.register(spec)
    got = registry.get("营收")
    assert got is not None
    assert got.metric == "营收"
    assert got.unit == "万元"
    assert "不含退货" in got.filters
    assert any(s.metric == "营收" for s in registry.list_all())


def test_register_same_metric_overwrites(registry: CaliberRegistry):
    """同名再注册 → 覆盖（幂等，不翻倍）。"""
    registry.register(_spec("营收", unit="万元"))
    registry.register(_spec("营收", unit="亿元"))
    all_specs = registry.list_all()
    assert len([s for s in all_specs if s.metric == "营收"]) == 1
    assert registry.get("营收").unit == "亿元"


def test_remove_then_absent(tmp_path: Path):
    """remove → 不在列表；再 remove 不报错。"""
    reg = CaliberRegistry(path=tmp_path / "c.json")
    reg.register(_spec("营收"))
    reg.remove("营收")
    assert reg.get("营收") is None
    reg.remove("营收")  # 不存在也不抛


def test_persists_across_instances(tmp_path: Path):
    """CRUD 落盘后，新实例能读到（进程间可见）。"""
    p = tmp_path / "caliber_registry.json"
    CaliberRegistry(path=p).register(_spec("营收"))
    assert p.exists()
    fresh = CaliberRegistry(path=p)
    assert fresh.get("营收") is not None


def test_get_missing_returns_none(registry: CaliberRegistry):
    assert registry.get("不存在的指标") is None


def test_list_all_empty(registry: CaliberRegistry):
    assert registry.list_all() == []


def test_json_file_is_valid_json(registry: CaliberRegistry):
    """落盘的是合法 JSON 数组，可直接对账。"""
    registry.register(_spec("营收"))
    registry.register(_spec("订单量", unit="万笔"))
    data = json.loads(Path(registry.path).read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert {d["metric"] for d in data} == {"营收", "订单量"}


# --------------------------------------------------------------------------- #
# 故障纪律：读故障不打断 caliber_check
# --------------------------------------------------------------------------- #
def test_registry_unreachable_skips_deviation(monkeypatch, tmp_path: Path):
    """注册表文件不可达 → caliber_check 跳过 caliber_deviation，不抛、不报。"""
    from app.core.agents.data_analyst.caliber import caliber_check
    from app.core.agents.data_analyst.state import AnalysisResult, ContextModel, Comparison, TimeRange

    bad = CaliberRegistry(path=tmp_path / "missing" / "deep" / "c.json")
    monkeypatch.setattr(
        "app.core.agents.data_analyst.caliber_registry._default_registry", lambda: bad)

    ctx = ContextModel(time_range=TimeRange(start="2024-03-01", end="2024-03-31"),
                       comparison=Comparison(type="环比", period=""))
    # 不会抛
    check = caliber_check(AnalysisResult(), ctx, report="营收 1.2 亿元")
    kinds = [i.kind for i in check.issues]
    # baseline_mismatch 仍由推断逻辑给（不依赖注册表）；caliber_deviation 应被跳过
    assert "caliber_deviation" not in kinds


# --------------------------------------------------------------------------- #
# API CRUD 端点
# --------------------------------------------------------------------------- #
@pytest.fixture()
def _api_registry(monkeypatch, tmp_path):
    """把默认注册表指向临时文件，隔离 API 测试。"""
    from app.core.agents.data_analyst import caliber_registry as mod

    reg = CaliberRegistry(path=tmp_path / "caliber_registry.json")
    monkeypatch.setattr(mod, "_default_registry", lambda: reg)
    return reg


def test_api_register_get_list_delete(_api_registry):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        # 列空
        assert c.get("/api/v1/chat/analyze/caliber").json() == []
        # 登记
        r = c.post("/api/v1/chat/analyze/caliber", json={
            "metric": "营收", "filters": ["不含退货"], "unit": "万元"})
        assert r.status_code == 201
        # 取单条
        r = c.get("/api/v1/chat/analyze/caliber?metric=营收")
        assert r.status_code == 200 and r.json()["metric"] == "营收"
        # 404
        assert c.get("/api/v1/chat/analyze/caliber?metric=不存在").status_code == 404
        # 覆盖
        c.post("/api/v1/chat/analyze/caliber",
               json={"metric": "营收", "unit": "亿元"})
        assert c.get("/api/v1/chat/analyze/caliber?metric=营收").json()["unit"] == "亿元"
        # 删除
        assert c.delete("/api/v1/chat/analyze/caliber?metric=营收").status_code == 200
        assert c.get("/api/v1/chat/analyze/caliber?metric=营收").status_code == 404


def test_api_rejects_empty_metric(_api_registry):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        # metric 空字符串 → 422（pydantic min_length=1）
        r = c.post("/api/v1/chat/analyze/caliber", json={"metric": ""})
        assert r.status_code == 422
