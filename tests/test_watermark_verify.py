"""E4/06 导出水印：issue ↔ verify 往返正确性 + 抗篡改。

Spec: docs/specs/E4/06-dlp-field-level.md §2

纪律：
- DLP_WATERMARK_SECRET 为空 → issue_watermark 返回 None（不出水印）
- token 本身可公开（不含 secret），校验只依赖 secret
- 任何篡改（payload / sig / 格式）必须返回 None
"""
from __future__ import annotations

from app.core.security.watermark import (
    issue_watermark,
    verify_watermark,
    watermark_lines,
)

SECRET = "test-secret-123"
USER = "user-abc"
SESSION = "session-xyz"
TS = "2026-09-17T12:00:00Z"


# --------------------------------------------------------------------------- #
# issue
# --------------------------------------------------------------------------- #
def test_issue_empty_secret_returns_null():
    """未设置密钥 → 不签发（零影响）。"""
    assert issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=""
    ) is None


def test_issue_returns_v1_format():
    token = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    assert token is not None
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[0] == "v1"
    # payload 段不应包含 user_id 明文（是 base64 过的 JSON）
    assert USER not in parts[1]
    # sig 段是 64 位 hex = sha256
    assert len(parts[2]) == 64


# --------------------------------------------------------------------------- #
# verify：正常周期
# --------------------------------------------------------------------------- #
def test_verify_roundtrip_happy_path():
    token = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    assert token is not None

    data = verify_watermark(token, SECRET)
    assert data is not None
    assert data["user_id"] == USER
    assert data["session_id"] == SESSION
    assert data["exported_at"] == TS


def test_verify_wrong_secret_returns_null():
    """token 不能跨密钥伪造。"""
    token = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    assert token is not None

    assert verify_watermark(token, "different-secret") is None


def test_verify_tampered_signature_returns_null():
    """篡改最后一段（签名）→ 立即被识别。"""
    token = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    parts = token.split(".")
    parts[2] = "0" * 64  # 伪造签名

    assert verify_watermark(".".join(parts), SECRET) is None


def test_verify_tampered_payload_returns_null():
    """篡改中间段（payload）但保留原签名 → 校验失败。

    攻击场景：把 base64 末字节替换但不动 sig —— HMAC 绑定的是原文，必然 miss。
    """
    token = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    parts = token.split(".")
    # base64 修改最后一位字母（XOR 一个字符）
    payload = parts[1]
    tam = ("A" if payload[-1] != "A" else "B")  # 保证不同
    parts[1] = payload[:-1] + tam

    assert verify_watermark(".".join(parts), SECRET) is None


def test_verify_wrong_version_returns_null():
    token = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    parts = token.split(".")
    parts[0] = "v2"

    assert verify_watermark(".".join(parts), SECRET) is None


def test_verify_malformed_token_returns_null():
    for bad in ["", "not-a-token", "v1.abc", "v1.abc.sig.extra", None]:
        assert verify_watermark(bad, SECRET) is None, bad


def test_verify_padding_handling():
    """base64 urlsafe 变体可能丢了 padding '='; verify 必须能自愈。

    让 payload 长度对 4 取余 ≠ 0：user_id 长度决定 padding 长度。
    """
    # 3-char user_id → base64 末尾无 padding
    tok = issue_watermark(
        user_id="ab", session_id=SESSION, exported_at=TS, secret=SECRET
    )
    assert tok is not None
    assert verify_watermark(tok, SECRET) is not None


# --------------------------------------------------------------------------- #
# watermark_lines（可读包裹）
# --------------------------------------------------------------------------- #
def test_watermark_lines_contains_required_fields():
    tok = issue_watermark(
        user_id=USER, session_id=SESSION, exported_at=TS, secret=SECRET
    )
    text = watermark_lines(tok, user_id=USER, session_id=SESSION, exported_at=TS)

    assert USER in text
    assert SESSION in text
    assert TS in text
    assert tok in text
    assert "HMAC-SHA256" in text
