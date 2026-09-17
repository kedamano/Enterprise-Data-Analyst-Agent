"""D51：报告图内嵌 —— 图生成了就要看得见（采集 / 内嵌 / 取图端点）。

Spec: docs/specs/C5/01-report-charts-ui.md

断在哪：`visualization` 工具把 PNG 落到会话工作目录，但**报告里没有任何引用**，
也没有能取图的端点——图生成了，用户永远看不到。

本文件用**手工构造的 PNG 字节**作为图源，不依赖 matplotlib：
`viz_tool` 的降级设计是"没装 matplotlib 就返回 ok=False"（本机 venv 正是如此），
所以"图从哪来"不是本卡的被测对象，"图有了之后能不能看见"才是。

四层：① 采集（只认真的画出来且还在会话目录里的图）② 内嵌（绝对 URL 的 `## 图表`）
③ 取图端点（白名单 + 目录校验 + 404/403）④ 写侧文件名消毒（LLM 给的 title 不消毒会写出目录外）。
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm

# 1x1 透明 PNG —— 真字节，不是占位符（FileResponse 要能当图片读出去）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """离线确定性环境：mock LLM + 无 Redis + 会话工作目录落到 tmp。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    workdir = tmp_path / "work"
    workdir.mkdir()
    # 会话工作目录落到 tmp：所有取用方都**运行期**导入该函数（charts/export 同范式）
    monkeypatch.setattr("app.core.tools.session_workdir", lambda sid: workdir)
    yield workdir
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def _chart_file(workdir: Path, name: str = "sales_chart.png") -> Path:
    p = workdir / name
    p.write_bytes(PNG_BYTES)
    return p


def _viz_result(step_id: str, path: Path, title: str = "营收图",
                ok: bool = True, status: str = "SUCCESS"):
    from app.core.agents.data_analyst.state import ToolResult

    return ToolResult(
        step_id=step_id, tool="visualization", status=status,
        input={"chart_type": "bar", "title": title},
        output={"ok": ok, "image_path": str(path), "chart_type": "bar", "points": 3},
        artifacts=[str(path)],
    )


def _state(*results):
    from app.core.agents.data_analyst.state import AgentState

    return AgentState(session_id="d51_charts", user_query="分析各区域营收",
                      tool_results=list(results))


# --------------------------------------------------------------------------- #
# 一、采集：只认「画出来了 + 还在会话目录里」
# --------------------------------------------------------------------------- #
def test_collect_charts_returns_successful_viz(env):
    from app.core.agents.data_analyst.charts import collect_charts

    p = _chart_file(env)
    charts = collect_charts(_state(_viz_result("viz_1", p)))
    assert len(charts) == 1
    assert charts[0]["name"] == "sales_chart.png"
    assert charts[0]["step_id"] == "viz_1"


def test_collect_charts_skips_failed_missing_and_outside(env, tmp_path):
    """三类"不算图"：失败步骤 / 文件已被删 / 路径在会话目录之外。"""
    from app.core.agents.data_analyst.charts import collect_charts

    ok_path = _chart_file(env, "ok.png")
    gone = env / "gone.png"          # 从未落盘
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)   # 真文件，但不在会话工作目录之下

    charts = collect_charts(_state(
        _viz_result("viz_ok", ok_path),
        _viz_result("viz_failed", ok_path, ok=False, status="FAILED"),
        _viz_result("viz_gone", gone),
        _viz_result("viz_outside", outside),
    ))
    assert [c["step_id"] for c in charts] == ["viz_ok"], \
        "失败/缺失/越界的图一律不入列表（引用一张裂图比没有图更糟）"


def test_collect_charts_empty_when_no_visualization(env):
    from app.core.agents.data_analyst.charts import collect_charts

    assert collect_charts(_state()) == []
    assert collect_charts(None) == []


# --------------------------------------------------------------------------- #
# 二、内嵌：报告末尾的 `## 图表`
# --------------------------------------------------------------------------- #
def test_embed_charts_appends_section_with_absolute_url(env):
    from app.core.agents.data_analyst.charts import collect_charts, embed_charts

    p = _chart_file(env, "sales_chart.png")
    charts = collect_charts(_state(_viz_result("viz_1", p, title="各区域营收")))
    report = embed_charts("# 报告\n\n正文。", charts, session_id="d51_charts")

    assert "## 图表" in report
    # 绝对 URL：报告会被复制到导出包/剪贴板，相对路径在别处必然裂图
    assert "![各区域营收](/api/v1/chat/analyze/chart/d51_charts/sales_chart.png)" in report
    assert report.startswith("# 报告"), "图在正文之后，不打断阅读"


def test_embed_charts_is_a_noop_without_charts(env):
    from app.core.agents.data_analyst.charts import embed_charts

    report = "# 报告\n\n正文。"
    assert embed_charts(report, [], session_id="s") == report, "无图绝不留一个空标题"


def test_embed_charts_export_mode_uses_relative_url(env):
    """导出包用 url_mode="export"：相对路径 `charts/{name}`，自包含、无绝对 chart 端点 URL。"""
    from app.core.agents.data_analyst.charts import (
        CHART_URL_PREFIX, collect_charts, embed_charts,
    )

    p = _chart_file(env, "sales_chart.png")
    charts = collect_charts(_state(_viz_result("viz_1", p, title="各区域营收")))
    report = embed_charts("# 报告\n\n正文。", charts, session_id="d51_charts",
                          url_mode="export")

    assert "![各区域营收](charts/sales_chart.png)" in report
    assert "/api/v1/chat/analyze/chart/" not in report
    assert report.startswith("# 报告")


def test_embed_charts_live_mode_is_default(env):
    """运行期默认 url_mode="live"：绝对 URL（chart 端点直出）。"""
    from app.core.agents.data_analyst.charts import collect_charts, embed_charts

    p = _chart_file(env, "sales_chart.png")
    charts = collect_charts(_state(_viz_result("viz_1", p, title="x")))
    # 不传 url_mode → 绝对路径
    report = embed_charts("# r", charts, session_id="d51_charts")
    assert "/api/v1/chat/analyze/chart/d51_charts/sales_chart.png" in report


def test_strip_charts_section_strips_trailing_section(env):
    from app.core.agents.data_analyst.charts import (
        collect_charts, embed_charts, strip_charts_section,
    )

    p = _chart_file(env, "sales_chart.png")
    charts = collect_charts(_state(_viz_result("viz_1", p, title="x")))
    full = embed_charts("# 报告\n\n正\n文。", charts, session_id="d51_charts")
    stripped = strip_charts_section(full)
    assert "## 图表" not in stripped
    assert stripped.rstrip().endswith("文。"), "只剥尾部图段，正文保留"
    # 原文末尾没 `\n## 图表` 时 → 原样返回
    assert strip_charts_section("# 正文") == "# 正文"


def test_reporter_embeds_chart_into_final_report(env):
    """端到端：跑 reporter 节点，报告里必须有图引用。"""
    from app.core.agents.data_analyst.nodes import run_reporter

    p = _chart_file(env, "sales_chart.png")
    state = _state(_viz_result("viz_1", p, title="各区域营收"))
    state.context.objective = "分析各区域营收"
    run_reporter(state)

    assert "## 图表" in state.report
    assert "/api/v1/chat/analyze/chart/d51_charts/sales_chart.png" in state.report


# --------------------------------------------------------------------------- #
# 三、取图端点
# --------------------------------------------------------------------------- #
def _client_for(state):
    from fastapi.testclient import TestClient

    from app.core.agents.data_analyst import checkpoint
    from app.main import app

    checkpoint.save(state)
    return TestClient(app)


def test_chart_endpoint_serves_the_png(env):
    p = _chart_file(env, "sales_chart.png")
    client = _client_for(_state(_viz_result("viz_1", p)))

    r = client.get("/api/v1/chat/analyze/chart/d51_charts/sales_chart.png")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == PNG_BYTES, "必须原样返回图，不能是摘要/占位"


@pytest.mark.parametrize("name", ["..%5Csecret.png", "..%5C..%5Csecret.png",
                                  "%2E%2E%5Csecret.png", "evil.txt", "evil.svg"])
def test_chart_endpoint_rejects_hostile_names_at_the_whitelist(env, name):
    """靠**白名单**挡下的四种（真的走到了处理函数，故必须是 400）：

    反斜杠穿越（URL 里 `\\` 是合法路径字符，能到达 handler）、非图片扩展名、
    svg（同源 inline 可带脚本 → 显式排除）。

    先验端点活着：路由不匹配的 404 与"端点挡住了"的 400 是两回事，
    不先确认端点存在，这条用例就是假绿。
    """
    outside = env.parent / "secret.png"
    outside.write_bytes(PNG_BYTES)
    workdir_png = _chart_file(env, "real.png")
    client = _client_for(_state(_viz_result("viz_1", workdir_png)))

    alive = client.get("/api/v1/chat/analyze/chart/d51_charts/real.png")
    assert alive.status_code == 200, "端点本身要活着，否则下面的 400 毫无意义（假绿）"

    r = client.get(f"/api/v1/chat/analyze/chart/d51_charts/{name}")
    assert r.status_code == 400, f"{name} 没有被白名单挡住：{r.status_code}"
    assert r.content != PNG_BYTES, "越界文件的内容绝不能出网"


@pytest.mark.parametrize("name", ["..%2Fsecret.png", "a%2Fb.png", "..%2F..%2Fsecret.png"])
def test_chart_endpoint_never_serves_files_outside_workdir(env, name):
    """含 `/` 的编码名由**路由层**挡下（404，到不了 handler）——保证同样是"拿不到文件"。

    这里不断言 400：换一层实现（比如自定义 path converter）数字会变，
    但**用户可见的后果**不变——越界文件一个字节都出不去。这才是要钉的东西。
    """
    outside = env.parent / "secret.png"
    outside.write_bytes(PNG_BYTES)
    workdir_png = _chart_file(env, "real.png")
    client = _client_for(_state(_viz_result("viz_1", workdir_png)))
    assert client.get("/api/v1/chat/analyze/chart/d51_charts/real.png").status_code == 200

    r = client.get(f"/api/v1/chat/analyze/chart/d51_charts/{name}")
    assert r.status_code != 200, f"{name} 竟然拿到了文件"
    assert r.content != PNG_BYTES, "越界文件的内容绝不能出网"


def test_chart_endpoint_404_when_missing(env):
    workdir_png = _chart_file(env, "real.png")
    client = _client_for(_state(_viz_result("viz_1", workdir_png)))
    r = client.get("/api/v1/chat/analyze/chart/d51_charts/nope.png")
    assert r.status_code == 404
    assert "图表" in r.json().get("detail", ""), \
        "必须是我们自己的 404（路由不存在的 404 也叫 404，分不出端点是否真的在）"


def test_chart_endpoint_403_for_other_users_session(env, monkeypatch):
    """AUTH 开启 + 无归属记录 → 403（与 trace/artifacts/export 同一条纪律）。"""
    import json

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", json.dumps([{"key": "k", "user_id": "u", "roles": ["analyst"]}]))
    get_settings.cache_clear()

    p = _chart_file(env, "sales_chart.png")
    client = _client_for(_state(_viz_result("viz_1", p)))
    r = client.get("/api/v1/chat/analyze/chart/d51_charts/sales_chart.png",
                   headers={"X-API-Key": "k"})
    assert r.status_code == 403
    assert r.content != PNG_BYTES


# --------------------------------------------------------------------------- #
# 四、写侧：文件名消毒（读侧白名单堵不住"写到目录外"）
# --------------------------------------------------------------------------- #
def test_filenames_are_sanitized_against_path_separators():
    """title 直接来自 LLM 参数 → 现在是**任意串**，不消毒就会写出会话目录。"""
    from app.core.tools.viz_tool import safe_chart_name

    assert "/" not in safe_chart_name("../../evil")
    assert "\\" not in safe_chart_name("..\\..\\evil")
    assert not safe_chart_name("../../evil").startswith(".")
    assert safe_chart_name("各区域营收") == "各区域营收", "中文标题必须原样保留"
    assert safe_chart_name("") == "chart", "空名要有确定兜底"
    assert safe_chart_name("...") == "chart", "纯点号等于没有名字"


@pytest.mark.parametrize("title", [
    "../../evil", "..\\..\\evil", "a..b", "...", "", "   ", "各区域营收",
    "a/b/c", "con", "nul", "x" * 200, "多轮/../销售 图",
])
def test_sanitized_names_always_pass_the_read_side_whitelist(title):
    """**写侧产物必须能过读侧白名单**——否则图落盘了却在报告/端点里静默消失。

    这条不是形式对称：第一版实现里，读侧禁了名字中的 `..`，而写侧允许
    （`a..b` → `a..b.png`）——两张白名单悄悄分叉，症状是"图生成了但看不见"，
    且只在标题含连续点时复现。参数化覆盖各类敌意标题后，分叉无处可藏。
    """
    from app.core.agents.data_analyst.charts import is_safe_chart_name
    from app.core.tools.viz_tool import safe_chart_name

    name = f"{safe_chart_name(title)}.png"
    assert is_safe_chart_name(name), f"title={title!r} → {name!r} 过不了读侧"


def test_viz_tool_writes_inside_workdir_even_with_hostile_title(env):
    """有 matplotlib 才谈得上落盘；没有则这条路本机跑不了——故此处只验消毒契约。"""
    from app.core.tools.viz_tool import safe_chart_name, _HAVE_MPL

    if not _HAVE_MPL:
        pytest.skip("本机未安装 matplotlib，viz_tool 按设计返回 ok=False（图生成非本卡被测对象）")
    from app.core.tools.viz_tool import run

    out = run({"data": [{"a": "x", "b": 1}], "x": "a", "y": "b",
               "title": "../../evil"}, workdir=str(env))
    assert out["ok"] is True, out
    written = Path(out["image_path"]).resolve()
    assert written.parent == env.resolve(), "图必须落在会话工作目录内"
    assert written.name == f"{safe_chart_name('../../evil')}.png"


# --------------------------------------------------------------------------- #
# 五、前端渲染：后端嵌的 `![图](url)` 得真被渲染出来
# --------------------------------------------------------------------------- #
_WEB = Path(__file__).resolve().parent.parent / "web"


def test_frontend_renders_report_images():
    """验收卡写的是「报告含图片引用**且前端渲染**」——两半都要钉。

    后端嵌好 `![alt](url)` 只是一半：Markdown 渲染器若不实现 `img`，
    URL 会以纯文本躺在报告里（用户看到一串地址，不是图）。
    本机无浏览器/无头运行器可用，故按源码级契约钉住（与 metric_cards 同范式）。
    """
    path = _WEB / "src" / "components" / "Markdown.tsx"
    assert path.exists(), "缺少 Markdown 渲染器"
    src = path.read_text(encoding="utf-8")

    at = src.find("img:")
    assert at != -1, "报告里的 ![图](...) 必须被渲染，否则图内嵌只有后端一半"
    # 只看 img 渲染器**自己的**那段（400 字足够覆盖一个 JSX 元素），
    # 免得"文件里别处有 <img>"也算过——那是另一种假绿
    block = src[at:at + 400]
    assert "<img" in block, "img 渲染器必须真的输出 <img>，否则 URL 只会以纯文本躺在报告里"
    assert "<a " not in block, "图不能被降级成链接（用户要看到图，不是再点一次）"


# --------------------------------------------------------------------------- #
# 六、中文标题必须真的画出来（真模型跑出来的缺陷）
# --------------------------------------------------------------------------- #
def test_chart_title_renders_without_missing_glyphs(tmp_path):
    """中文标题不能渲染成方块（tofu）。

    **真模型跑 `test_agent_real.py::test_visualization_runs` 时暴露**：
    matplotlib 默认 sans-serif 首位是 DejaVu Sans，**不含 CJK 字形**，
    所以图是"成功生成了"，但标题是一排空心方框——图能取到、能内嵌、能进导出包，
    内容是废的。D51 刚把"图能被看见"这条链打通，这条链上运的却是废图。

    判据不看 `_CJK_FONT` 变量（那是实现细节），看**渲染时有没有缺字形告警**——
    换任何字体方案，只要中文画得出来就算过。
    """
    pytest.importorskip("matplotlib")
    import warnings

    from app.core.tools import viz_tool

    if viz_tool._CJK_FONT is None:  # pragma: no cover - 本机与 CI 均有
        pytest.skip("本机无任何 CJK 字体，中文渲染无从验证（属环境条件，非缺陷）")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = viz_tool.run(
            {"data": [{"a": "华北", "b": 1}, {"a": "华南", "b": 2}],
             "x": "a", "y": "b", "title": "各区域营收", "chart_type": "bar"},
            workdir=str(tmp_path),
        )

    assert out["ok"] is True, out
    missing = [str(w.message) for w in caught if "missing from font" in str(w.message)]
    assert not missing, f"中文标题被画成了方块（缺字形）：{missing[:3]}"
