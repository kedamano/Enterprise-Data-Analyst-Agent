"""E4/06 DLP 残留：水印verify端点。

Spec: docs/specs/E4/06-dlp-field-level.md §2

导出包里的 ``WATERMARK.txt`` 写明"用持有密钥的服务端校验"。本端点就是那个
**持有密钥的服务端**——secret 只从 ``DLP_WATERMARK_SECRET`` 读取，**绝不接受客户端传入**
（否则等于把密钥校验变成"客户端告诉服务端 secret 对不对"，毫无安全可言）。

两种调用方式覆盖两种场景：

- ``POST /security/watermark/verify``（JSON body ``{token}``）— 内部运营/审计即时校验；
- ``GET  /security/watermark/verify?token=...`` — ``WATERMARK.txt`` 里贴的离线校验链接
  （接收方点开即可知真伪，无需另装工具）。

两种方式**同源**同一个 :func:`_verify`：签名对 → 200 + payload；签名错/篡改/格式错 → 422。
HTTP 码区分"格式/参数错"与"校验失败"，便于自动化脚本分流。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/security", tags=["security"])


class _VerifyReq(BaseModel):
    token: str


def _verify(token: str) -> tuple[bool, dict | None, str]:
    """校验水印 token → ``(ok, payload_or_None, reason)``。

    纯函数、不调 LLM、不发网络；异常一律收敛为 ``(False, None, reason)``，
    绝不因校验逻辑抛 500 而把内部错误暴露给调用方。
    """
    from ...config import get_settings
    from ...core.security.watermark import verify_watermark

    settings = get_settings()
    secret = getattr(settings, "dlp_watermark_secret", "") or ""
    if not secret:
        # 未配 secret → 校验能力不存在，**不是**"token 无效"。
        # 用 409（Conflict）表达"该服务未启用此能力"，与 422"校验失败"区分。
        raise HTTPException(status_code=409, detail="未配置 DLP_WATERMARK_SECRET，校验不可用")

    if not token or not isinstance(token, str):
        return False, None, "token 为空或格式错误"
    try:
        data = verify_watermark(token, secret)
    except Exception as exc:
        return False, None, f"校验异常: {exc}"
    if data is None:
        return False, None, "签名无效或 token 已被篡改"
    return True, data, ""


@router.post("/watermark/verify")
def verify_watermark_post(req: _VerifyReq) -> dict:
    """JSON body 校验水印（内部运营/审计用）。"""
    ok, data, reason = _verify(req.token)
    if ok:
        return {"ok": True, "watermark": data}
    # 422：请求格式合法但**校验未通过**（签名错/篡改）。
    raise HTTPException(status_code=422, detail={"ok": False, "reason": reason})


@router.get("/watermark/verify")
def verify_watermark_get(token: str = Query(..., min_length=8, description="待校验的水印 token")) -> dict:
    """URL query 校验水印（WATERMARK.txt 里的离线校验链接）。"""

    ok, data, reason = _verify(token)
    if ok:
        return {"ok": True, "watermark": data}
    raise HTTPException(status_code=422, detail={"ok": False, "reason": reason})
