"""The /ui console is served and its bundled assets point at the stream endpoint.

注意：这是 Vite SPA —— index.html 是空壳（<div id="root"></div>），真正的 UI
文案在 React 挂载后才出现。所以本测试只验证「壳 + 资源 + 编译期常量」三个
层级的契约，不去断言挂载后的 DOM（那属于 Playwright/E2E 测试）。
"""
from __future__ import annotations

import re
from pathlib import Path


def test_ui_page_served():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # 1) SPA shell：必须有挂载点。
    assert 'id="root"' in body
    # 2) Vite 编译后的 bundle 引用必须存在（占位 script/src 链接）。
    assert re.search(r"/assets/index-[A-Za-z0-9_-]+\.(?:js|css)", body), body
    # 3) 文档标题体现产品名（中文/英文至少出现其一，方便用户从收藏/历史找回来）。
    assert ("企业数据分析智能体" in body) or ("Data Analyst Agent" in body)


def test_stream_endpoint_referenced_in_bundle_or_source():
    """编译产物或前端源码必须引用真实 SSE 端点，避免出现"前端写死死端点"型 Bug。"""
    web_root = Path(__file__).resolve().parent.parent / "web"
    candidates = list((web_root / "src").rglob("*.ts")) + list(
        (web_root / "src").rglob("*.tsx")
    )
    blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in candidates)
    assert "/api/v1/chat/analyze/stream" in blob, (
        "前端代码必须引用 SSE 端点 /api/v1/chat/analyze/stream，"
        "否则 Agent 拿不到任何事件流。"
    )
