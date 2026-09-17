"""E7/02 数据源管理 API —— 页面「新建连接」的服务端。

Spec: docs/specs/E7/01-multi-source-deploy.md 延伸。

- ``POST /datasources/test``：按表单构造 DSN → ``SELECT 1`` 探活（5s 超时）。
- ``POST /datasources``：**探活通过才落盘**——存一个连不上的源只会让
  每次取数都失败；名称与 env 配置的源冲突时拒绝（静默覆盖会让人困惑）。
- ``DELETE /datasources/{name}``：只允许删页面新建的（local）；env 配置的
  指回 ``.env`` 的 ``DATA_SOURCES``。

安全：
- 响应里的 URL 一律走 ``_mask_dsn`` 脱敏，密码绝不回显。
- SQLite 文件不存在时**直接拒绝**，绝不让 SQLAlchemy 静默创建空库
  （与 dbguard 同哲学：空库会让分析报告看起来像真的）。
- 写入操作仍受全局 ``sql_readonly`` 守卫约束——这里只是「接入」，不是「写入」。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import create_engine, text

from ...config import get_settings
from ...core.tools.datasource import _parse_sources
from ...core.tools.datasource_store import DataSourceStore, build_url
from ...models.schemas import (
    DataSourceConn,
    DataSourceCreateRequest,
    DataSourceDeleteResponse,
    DataSourceTestRequest,
    DataSourceTestResponse,
)
from .health import _mask_dsn

router = APIRouter()

_PROBE_TIMEOUT_S = 5.0


def _store() -> DataSourceStore:
    return DataSourceStore(get_settings().datasource_store_path)


def _form_to_url(req: DataSourceTestRequest) -> str:
    return build_url(
        req.dialect,
        host=req.host,
        port=req.port,
        database=req.database,
        username=req.username,
        password=req.password,
        path=req.path,
    )


def _probe(url: str) -> None:
    """探活：``SELECT 1``，5 秒超时。失败抛异常（消息可读）。

    SQLite 先查文件存在性——SQLAlchemy 首连会**静默创建空库文件**，
    探活绝不能当creator。
    """
    if url.startswith("sqlite"):
        p = url.split("///", 1)[-1]
        if not p or not Path(p).exists():
            raise RuntimeError(f"数据库文件不存在：{p or url}")
        engine = create_engine(url, connect_args={"timeout": _PROBE_TIMEOUT_S})
    else:
        connect_args = (
            {"options": f"-c statement_timeout={int(_PROBE_TIMEOUT_S * 1000)}"}
            if url.startswith("postgres")
            else {}
        )
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()


@router.post("/datasources/test", response_model=DataSourceTestResponse, tags=["datasources"])
def test_datasource(req: DataSourceTestRequest):
    """「新建连接」弹窗的「测试连接」按钮：只探活，不落盘。"""
    try:
        url = _form_to_url(req)
    except ValueError as exc:
        return DataSourceTestResponse(ok=False, error=str(exc))
    try:
        _probe(url)
    except Exception as exc:
        return DataSourceTestResponse(ok=False, error=str(exc)[:300])
    return DataSourceTestResponse(ok=True, message="连接成功")


@router.post("/datasources", response_model=DataSourceConn, status_code=201, tags=["datasources"])
def create_datasource(req: DataSourceCreateRequest):
    """新建连接：校验 → 探活 → 落盘。返回脱敏后的连接配置。"""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="连接名称不能为空")
    if name.lower() == "default":
        raise HTTPException(status_code=400, detail='"default" 是主数据源保留名，请换一个名称')
    settings = get_settings()
    env_names = set(_parse_sources(settings.data_sources))
    if name in env_names:
        raise HTTPException(
            status_code=400,
            detail=f"名称 {name!r} 已由服务端 DATA_SOURCES 配置占用，请换一个名称",
        )
    try:
        url = _form_to_url(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        _probe(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"连接失败：{str(exc)[:300]}")
    try:
        _store().add(name, url, req.dialect.strip().lower())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return DataSourceConn(
        name=name,
        dialect=req.dialect.strip().lower(),
        url=_mask_dsn(url),
        readonly=settings.sql_readonly,
        origin="local",
    )


@router.delete("/datasources/{name}", response_model=DataSourceDeleteResponse, tags=["datasources"])
def delete_datasource(name: str):
    """删除页面新建的连接。env 配置的源不在本地存储里，指回 ``.env``。"""
    if name not in _store().list_entries():
        if name in set(_parse_sources(get_settings().data_sources)):
            raise HTTPException(
                status_code=400,
                detail="该连接由服务端 DATA_SOURCES 环境变量配置，请修改 .env 后重启服务",
            )
        raise HTTPException(status_code=404, detail="连接不存在")
    removed = _store().remove(name)
    return DataSourceDeleteResponse(ok=removed, name=name)
