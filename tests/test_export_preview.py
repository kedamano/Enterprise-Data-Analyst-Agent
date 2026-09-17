"""D51：导出预览 —— **预览说是什么，包里就必须是什么**。

Spec: docs/specs/C5/01-report-charts-ui.md §3

断在哪：交付包（E5/03）只能盲点——用户不知道包里有什么（几份 CSV？有没有图？脱敏了没？），
点完才发现不是自己要的。

本卡最重要的一条纪律：**manifest 的 files 与 zip 的 namelist 必须由同一个构造函数产出**。
两处各写一份清单 = 预览迟早骗人（且没人会立刻发现）。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000001f49444154789c6360000002000100f5a2b7d00000"
    "000049454e44ae426082"
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    monkeypatch.setenv("DLP_POLICY", "")
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr("app.core.tools.session_workdir", lambda sid: workdir)
    yield workdir
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def _run(session_id: str, csv_rows=3):
    """跑一轮真分析（mock LLM），并挂上一张图 + 一份 CSV 产物。"""
    from app.core.agents.data_analyst import checkpoint
    from app.core.agents.data_analyst.graph import run_analysis
    from app.core.tools import session_workdir

    state = run_analysis(session_id, "分析各区域营收")
    wd = session_workdir(session_id)
    (wd / "sales_chart.png").write_bytes(PNG_BYTES)
    (wd / f"{session_id}.csv").write_text("region,revenue\n华东,1.2\n华北,0.8\n",
                                          encoding="utf-8")
    state.tool_results.append(_viz(state, wd))
    checkpoint.save(state)
    return state


def _viz(state, wd):
    from app.core.agents.data_analyst.state import ToolResult

    return ToolResult(step_id="viz_1", tool="visualization", status="SUCCESS",
                      output={"ok": True, "image_path": str(wd / "sales_chart.png")},
                      artifacts=[str(wd / "sales_chart.png")])


def _zip_names(client, session_id: str, masked: str = "") -> list[str]:
    q = f"?format=zip&masked={masked}" if masked else "?format=zip"
    r = client.get(f"/api/v1/chat/analyze/export/{session_id}{q}")
    assert r.status_code == 200, r.text[:200]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return zf.namelist()


def _manifest(client, session_id: str, masked: str = "") -> dict:
    q = f"?masked={masked}" if masked else ""
    r = client.get(f"/api/v1/chat/analyze/export/{session_id}/manifest{q}")
    assert r.status_code == 200, r.text[:200]
    return r.json()


# --------------------------------------------------------------------------- #
# 一、同源：预览 == 包
# --------------------------------------------------------------------------- #
def test_manifest_matches_zip_namelist_exactly(env):
    """本卡的核心契约：**逐项相等**，不是"都有 report.md"这种弱断言。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_prev")
    files = _manifest(client, "d51_prev")["files"]
    assert [f["name"] for f in files] == sorted(_zip_names(client, "d51_prev"))


def test_manifest_bytes_match_real_file_sizes(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_prev_sz")
    files = _manifest(client, "d51_prev_sz")["files"]
    assert files, "空清单说明 manifest 没真的构建产物"
    assert all(f["bytes"] > 0 for f in files)
    # 报告的字节数必须与真实字节数一致（前端要显示"多少 KB"）
    import io as _io
    import zipfile as _zip

    r = client.get("/api/v1/chat/analyze/export/d51_prev_sz?format=zip")
    with _zip.ZipFile(_io.BytesIO(r.content)) as zf:
        assert next(f for f in files if f["name"] == "report.md")["bytes"] == \
            len(zf.read("report.md"))


def test_manifest_reports_total_bytes(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_prev_tot")
    m = _manifest(client, "d51_prev_tot")
    assert m["total_bytes"] == sum(f["bytes"] for f in m["files"])


def test_manifest_404_for_unknown_session(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    # 先验端点活着：路由不匹配的 404 与"会话不存在"的 404 长得一样，不区分就是假绿
    _run("d51_prev_live")
    assert _manifest(client, "d51_prev_live")["files"]

    r = client.get("/api/v1/chat/analyze/export/no_such_session/manifest")
    assert r.status_code == 404
    assert "未找到运行记录" in r.json().get("detail", "")


# --------------------------------------------------------------------------- #
# 二、图表入包；脱敏版**不含图**（位图无法逐像素脱敏）
# --------------------------------------------------------------------------- #
def test_zip_includes_charts_when_present(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_chart_zip")
    names = _zip_names(client, "d51_chart_zip")
    assert "charts/sales_chart.png" in names, names
    assert "charts/sales_chart.png" in [f["name"] for f in _manifest(client, "d51_chart_zip")["files"]]


def test_masked_export_excludes_charts_and_says_so(env, monkeypatch):
    """脱敏版**不能**夹带无法脱敏的位图；README/manifest 都要写明原因。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    state = _run("d51_chart_masked")

    names = _zip_names(client, "d51_chart_masked", masked="1")
    assert not any(n.startswith("charts/") for n in names), names

    r = client.get("/api/v1/chat/analyze/export/d51_chart_masked?format=zip&masked=1")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        readme = zf.read("README.txt").decode("utf-8")
    assert "图表" in readme and "脱敏" in readme, "必须写明图为什么不进脱敏包"

    m = _manifest(client, "d51_chart_masked", masked="1")
    assert m["masked"] is True
    assert not any(f["name"].startswith("charts/") for f in m["files"])


def test_unmasked_export_keeps_charts_and_default_masked_flag_is_reported(env):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_chart_raw")
    m = _manifest(client, "d51_chart_raw")
    # DLP_POLICY 空 → 策略未激活 → 缺省非脱敏（与 zip 端既有行为一致）
    assert m["masked"] is False
    assert "charts/sales_chart.png" in [f["name"] for f in m["files"]]


# --------------------------------------------------------------------------- #
# 三、预览**不需要**两步授权（它只暴露文件名与大小，不暴露任何值）
# --------------------------------------------------------------------------- #
def test_manifest_needs_no_hitl_grant(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_prev_hitl")
    monkeypatch.setenv("HITL_ENABLED", "true")
    get_settings.cache_clear()

    # 同一会话的**原始值导出**被 428 拦下（对照）……
    blocked = client.get("/api/v1/chat/analyze/export/d51_prev_hitl?format=zip&masked=0")
    assert blocked.status_code == 428, "对照：原始导出本就该被两步授权拦下"
    # ……而预览放行（不含任何原始值）
    assert _manifest(client, "d51_prev_hitl", masked="0")["files"]


# --------------------------------------------------------------------------- #
# 四、前端接线契约
# --------------------------------------------------------------------------- #
_WEB = Path(__file__).resolve().parent.parent / "web"


def _read(rel: str) -> str:
    return (_WEB / rel).read_text(encoding="utf-8")


def test_export_preview_component_exists_and_is_rendered():
    assert (_WEB / "src/components/ExportPreview.tsx").exists(), "缺少导出预览组件"
    chat = _read("src/components/ChatMessage.tsx")
    assert "ExportPreview" in chat, "组件必须真的被渲染，否则是死代码"


def test_export_preview_hits_the_manifest_endpoint_via_api_client():
    prev = _read("src/components/ExportPreview.tsx")
    assert "manifest" in prev, "必须指向真实 manifest 端点"
    assert "fetchManifest" in prev or "from \"@/lib/api\"" in prev, \
        "请求必须经 api.ts（带 authHeaders），不得裸 fetch（裸 fetch 会整站 401）"
    assert "fetch(" not in prev, "组件内不得直接 fetch"

    api = _read("src/lib/api.ts")
    assert "/manifest" in api, "api.ts 里必须真的有这个请求"
    assert "authHeaders()" in api


def test_export_preview_download_link_matches_preview_mask_mode():
    """预览的是脱敏版，下载就必须是脱敏版——不能"看了 A 下到 B"。"""
    prev = _read("src/components/ExportPreview.tsx")
    assert "analyze/export/" in prev, "缺下载链接"
    assert "masked=" in prev, "下载链接必须显式带 masked，与预览一致"
    assert "format=zip" in prev


def test_export_preview_surfaces_errors_instead_of_silence():
    prev = _read("src/components/ExportPreview.tsx")
    assert "catch" in prev, "请求失败必须被接住"
    assert "error" in prev.lower(), "失败要显示出来，不能静默"


# --------------------------------------------------------------------------- #
# 四、导出报告自包含：图以相对路径引用（离线可看），不泄漏绝对 chart 端点 URL
# --------------------------------------------------------------------------- #
def test_unmasked_export_report_uses_relative_chart_urls(env):
    """导出包的 report.md 图引用必须是相对路径 `charts/{name}`——自包含。

    运行期 report 内嵌的是绝对 `/api/v1/chat/analyze/chart/...` URL（chart 端点直出），
    但导出包脱离服务就裂图；所以 _build_items 必须按 url_mode="export" 重嵌为相对路径。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_export_rel")
    r = client.get("/api/v1/chat/analyze/export/d51_export_rel")
    assert r.status_code == 200, r.text[:200]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        report = zf.read("report.md").decode("utf-8")
        names = zf.namelist()
    assert "charts/sales_chart.png" in names, names
    # 必须是相对路径 `charts/<name>` —— 自包含（alt 文本由运行时 viz title 决定，不强匹配）。
    assert re.search(r"!\[[^\]]*\]\(charts/sales_chart\.png\)", report), report
    assert "/api/v1/chat/analyze/chart/" not in report, "泄漏了运行期的绝对图 URL"


def test_masked_export_report_has_no_chart_section(env, monkeypatch):
    """脱敏版不含图 → report.md 也**不带** `## 图表` 段（离线看不到位图）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    _run("d51_export_mask")
    r = client.get("/api/v1/chat/analyze/export/d51_export_mask?masked=1")
    assert r.status_code == 200, r.text[:200]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        report = zf.read("report.md").decode("utf-8")
    assert "## 图表" not in report, "脱敏版报告不得内嵌图表段"
    assert "charts/" not in report
