"""P2-2 图片视觉接入：确定性层验证（不依赖真实 LLM 凭证）。

覆盖：
1. 附件层：build_preview 对图片返回 kind=image；store.describe 提示用 image_analyze；
   attached_images 只列图片。
2. 上传路由：图片落地到磁盘并写入 path（用 TestClient 跑真实 /attachments/upload）。
3. 工具：image_analyze 在 MOCK_LM 下成功提取描述；缺图/未传 image 参数 → FAILED。
4. 编排：构造含 image_analyze 的计划经 run_executor_all 成功命中落地图片。
5. MockLLM planner：存在 uploaded_images 时首步发出 image_analyze。
"""
from __future__ import annotations

import base64
import uuid

import pytest

# 1x1 透明 PNG（base64），用于构造真实可读的图片字节
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLv"
    "AAAAAElFTkSuQmCC"
)
PNG_BYTES = base64.b64decode(_PNG_B64)


@pytest.fixture
def mock_llm(monkeypatch):
    """强制走 MockLLM（离线），避免依赖真实视觉凭证。

    conftest 在每个测试前会 ``get_settings.cache_clear()``，所以这里设的
    ``MOCK_LLM=1`` 会在首次 ``get_settings()`` 时生效（use_mock_llm → True）。
    """
    monkeypatch.setenv("MOCK_LLM", "1")
    from app.infrastructure.llm import router as r
    r.reset_llm()


# --------------------------------------------------------------------------- #
# 1. 附件层
# --------------------------------------------------------------------------- #

def test_build_preview_returns_image_kind():
    from app.core.attachments import build_preview, is_image

    assert is_image("chart.png")
    assert not is_image("data.csv")
    p = build_preview(PNG_BYTES, "chart.png")
    assert p.kind == "image"
    assert p.bytes == len(PNG_BYTES)
    assert p.path == ""  # 落地由上传路由负责，build_preview 不持有 session_id


def test_store_describe_includes_image_analyze():
    from app.core.attachments import DatasetPreview, get_attachment_store

    sid = "img-ctx-" + uuid.uuid4().hex[:8]
    store = get_attachment_store()
    store.clear(sid)
    try:
        store.put(sid, DatasetPreview(
            name="chart.png", kind="image", bytes=len(PNG_BYTES),
            path="/tmp/x/chart.png",
        ))
        text = store.describe(sid)
        assert "chart.png" in text
        assert "image_analyze" in text
        assert "实际读取图片" in text
    finally:
        store.clear(sid)


def test_attached_images_only_lists_images():
    from app.core.attachments import (
        DatasetPreview, attached_images, get_attachment_store,
    )

    sid = "img-list-" + uuid.uuid4().hex[:8]
    store = get_attachment_store()
    store.clear(sid)
    try:
        store.put(sid, DatasetPreview(
            name="chart.png", kind="image", bytes=10, path="/tmp/c.png"))
        store.put(sid, DatasetPreview(
            name="t.csv", kind="table", rows=3, columns=["a"],
            sample=[{"a": 1}], all_rows=[{"a": 1}]))
        imgs = attached_images(sid)
        assert [i["name"] for i in imgs] == ["chart.png"]
    finally:
        store.clear(sid)


# --------------------------------------------------------------------------- #
# 2. 上传路由：真实落地 + path
# --------------------------------------------------------------------------- #

def test_upload_route_persists_image_and_path(mock_llm):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    sid = "img-route-" + uuid.uuid4().hex[:8]
    resp = client.post(
        "/api/v1/attachments/upload",
        files={"file": ("chart.png", PNG_BYTES, "image/png")},
        data={"session_id": sid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["attachment"]["kind"] == "image"
    # 落地路径应真实存在且可读
    assert body["attachment"].get("path")
    from pathlib import Path
    p = Path(body["attachment"]["path"])
    assert p.exists()
    assert p.read_bytes() == PNG_BYTES
    assert "image_analyze" in (body.get("hint") or "")


# --------------------------------------------------------------------------- #
# 3. image_analyze 工具
# --------------------------------------------------------------------------- #

def test_vision_tool_mock_success(mock_llm):
    from app.core.attachments import DatasetPreview, get_attachment_store, image_dir
    from app.core.tools import execute_tool

    sid = "img-tool-" + uuid.uuid4().hex[:8]
    store = get_attachment_store()
    store.clear(sid)
    try:
        d = image_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "chart.png"
        path.write_bytes(PNG_BYTES)
        store.put(sid, DatasetPreview(
            name="chart.png", kind="image", bytes=len(PNG_BYTES), path=str(path)))

        res = execute_tool("s1", "image_analyze",
                           {"image": "chart.png", "question": "图里有什么数据?"}, sid)
        assert res.status == "SUCCESS", res.error
        out = res.output
        assert out["ok"] is True
        # MockLLM 返回的是具体数值（可当证据），不是"未获取到事实数据"
        assert "Region A" in out["description"]
        assert out["image"] == "chart.png"
    finally:
        store.clear(sid)


def test_vision_tool_missing_image_fails(mock_llm):
    from app.core.tools import execute_tool

    res = execute_tool("s2", "image_analyze",
                       {"image": "nope.png"}, "img-tool-missing")
    assert res.status == "FAILED"
    assert "未找到图片附件" in (res.error or "")


def test_vision_tool_missing_param_fails(mock_llm):
    from app.core.tools import execute_tool

    res = execute_tool("s3", "image_analyze", {}, "img-tool-noparam")
    assert res.status == "FAILED"
    assert "缺少参数 image" in (res.error or "")


# --------------------------------------------------------------------------- #
# 4. 编排端到端：run_executor_all 命中落地图片
# --------------------------------------------------------------------------- #

def test_e2e_image_analyze_executor(mock_llm):
    import uuid as _u

    from app.core.agents.data_analyst.nodes import run_executor_all
    from app.core.agents.data_analyst.state import AgentState, PlanModel, PlanStep
    from app.core.attachments import DatasetPreview, get_attachment_store, image_dir

    sid = "img-e2e-" + _u.uuid4().hex[:10]
    store = get_attachment_store()
    store.clear(sid)
    try:
        d = image_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "chart.png"
        path.write_bytes(PNG_BYTES)
        store.put(sid, DatasetPreview(
            name="chart.png", kind="image", bytes=len(PNG_BYTES), path=str(path)))

        plan = PlanModel(goal="g", steps=[
            PlanStep(id="step_0", objective="识别图片", action="a", tool="image_analyze",
                     dependencies=[], input={"image": "chart.png",
                                            "question": "提取图中数据"}),
        ])
        st = AgentState(session_id=sid, user_query="这张图反映了什么?", plan=plan)
        run_executor_all(st)

        by_id = {r.step_id: r for r in st.tool_results}
        assert by_id["step_0"].status == "SUCCESS", by_id["step_0"].error
        assert "Region A" in by_id["step_0"].output["description"]
    finally:
        store.clear(sid)


# --------------------------------------------------------------------------- #
# 5. MockLLM planner：存在图片 → 首步 image_analyze
# --------------------------------------------------------------------------- #

def test_mockllm_planner_emits_image_step(mock_llm):
    from app.infrastructure.llm.router import MockLLM

    user = (
        "<task_context>\n"
        '{"objective":"分析图中趋势","uploaded_images":'
        '[{"name":"chart.png","bytes":10,"path":"/tmp/chart.png"}]}\n'
        "</task_context>"
    )
    plan = MockLLM()._stage_planner(user)
    assert plan["steps"][0]["tool"] == "image_analyze", plan["steps"][0]
    assert plan["steps"][0]["input"]["image"] == "chart.png"
