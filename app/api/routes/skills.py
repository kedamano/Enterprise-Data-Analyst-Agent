"""技能（Skills）管理面 REST 接口。

Spec: 见 `docs/对标企业级Gap.md` §八 —— 技能管理 + 对话中选择技能增强大模型。

- `GET    /skills`             —— 清单（不含正文，列表页够用）
- `POST   /skills`             —— 新建（名称/描述/正文）
- `GET    /skills/{id}`        —— 详情（含正文，编辑页用）
- `PUT    /skills/{id}`        —— 更新（局部字段）
- `DELETE /skills/{id}`        —— 删除（连带目录）
- `POST   /skills/{id}/enabled`—— 启停（`.meta.json` 里的本机状态）
- `POST   /skills/import`      —— zip 导入（`SKILL.md` 目录形态，兼容 Anthropic 约定）

`skills_enabled=false` 时统一 503（配置关掉要吵，不静默空跑）——与 MCP 接入层同款取舍。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/skills", tags=["skills"])

_DISABLED = HTTPException(status_code=503, detail="技能管理未启用（skills_enabled=false）")


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    body: str = Field(default="", description="技能正文（Markdown，注入大模型的指令）")
    enabled: bool = True


class SkillUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=500)
    body: Optional[str] = None
    enabled: Optional[bool] = None


class EnabledBody(BaseModel):
    enabled: bool = True


def _enabled() -> bool:
    from ...config import get_settings

    return bool(getattr(get_settings(), "skills_enabled", True))


def _store():
    from ...core.skills import get_skill_store

    return get_skill_store()


@router.get("")
def list_skills() -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    skills = _store().list(include_body=False)
    return {"skills": skills, "total": len(skills)}


@router.post("", status_code=201)
def create_skill(req: SkillCreate) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    try:
        return _store().create(req.name, req.description, req.body, enabled=req.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import")
async def import_skills(file: UploadFile = File(...)) -> dict[str, Any]:
    """zip 导入。返回 `{imported: [...], errors: [...]}`——单个技能失败不中断其余。"""
    if not _enabled():
        raise _DISABLED
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    result = _store().import_zip(raw)
    if not result["imported"] and result["errors"]:
        # 全失败 → 400，把原因直接回给界面（否则用户只看到"导入成功 0 个"）
        raise HTTPException(status_code=400, detail="；".join(result["errors"]))
    return result


@router.get("/{skill_id}")
def get_skill(skill_id: str) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    item = _store().get(skill_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"技能不存在: {skill_id}")
    return item


@router.put("/{skill_id}")
def update_skill(skill_id: str, req: SkillUpdate) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    item = _store().update(skill_id, name=req.name, description=req.description,
                           body=req.body, enabled=req.enabled)
    if item is None:
        raise HTTPException(status_code=404, detail=f"技能不存在: {skill_id}")
    return item


@router.delete("/{skill_id}")
def delete_skill(skill_id: str) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    if not _store().delete(skill_id):
        raise HTTPException(status_code=404, detail=f"技能不存在: {skill_id}")
    return {"ok": True, "id": skill_id}


@router.post("/{skill_id}/enabled")
def set_skill_enabled(skill_id: str, body: EnabledBody) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    item = _store().set_enabled(skill_id, body.enabled)
    if item is None:
        raise HTTPException(status_code=404, detail=f"技能不存在: {skill_id}")
    return item
