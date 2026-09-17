"""E4/06 导出水印（可验证、不可伪造）— D49。

Spec: docs/specs/E4/06-dlp-field-level.md §2

水印让每一份导出去向可追：谁（user_id）、哪个会话、何时导出。
用 **HMAC-SHA256(secret, payload)** 签名——只有持有 secret 才签得出，
故可被接收方**验证真伪**，而不是一行"本文件由 X 导出"的断言。

纪律：
- ``DLP_WATERMARK_SECRET`` 为空 → ``issue_watermark`` 返回 ``None``（**不出水印**，默认零影响）；
- secret **不进 token 本身**、不落日志/审计明文；
- token 本身可公开（不含 secret），校验只依赖 secret；
- 水印是**附加**（新增文件 + 响应头），不改动任何既有产物字节。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Optional

_VERSION = "v1"


def issue_watermark(*, user_id: str, session_id: str, exported_at: str,
                    secret: str) -> Optional[str]:
    """签发水印 token：``v1.<base64(payload)>.<hex hmac>``。

    ``secret`` 为空 → ``None``（不签发）。
    """
    if not secret:
        return None
    payload = {
        "user_id": str(user_id or ""),
        "session_id": str(session_id or ""),
        "exported_at": str(exported_at or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_VERSION}.{b64}.{sig}"


def verify_watermark(token: Optional[str], secret: str) -> Optional[dict[str, Any]]:
    """校验并解析水印 token；签名不对 / 篡改 / 格式错 → ``None``。"""
    if not secret or not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    version, b64, sig = parts
    if version != _VERSION:
        return None
    # 补回被去掉的 base64 padding（urlsafe 编码可能丢了 "="）
    padded = b64 + "=" * (-len(b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    expected = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    # 常量时间比较：避免时序侧信道
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def watermark_lines(token: str, *, user_id: str, session_id: str,
                    exported_at: str) -> str:
    """zip 包内 ``WATERMARK.txt`` 的内容：人可读说明 + token（便于离线校验）。"""
    return (
        "本交付包导出水印\n"
        "==================\n"
        f"user_id:     {user_id}\n"
        f"session_id:  {session_id}\n"
        f"exported_at: {exported_at}\n"
        "\n"
        "校验方式：用持有密钥的服务端对下面 token 做 HMAC-SHA256 校验\n"
        "（app.core.security.watermark.verify_watermark）。\n"
        "token 本身不含密钥，可安全随包分发。\n"
        "\n"
        f"{token}\n"
    )
