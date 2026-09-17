"""D51：报告图内嵌 —— 采集会话里**真实存在**的图，并嵌进报告。

Spec: docs/specs/C5/01-report-charts-ui.md §1

断在哪：`visualization` 工具把 PNG 落到会话工作目录（`data/artifacts/<sid>/`），
但报告里没有任何引用，也没有能取图的端点——**图生成了，用户永远看不到**。

纪律（与 E5/03 导出同一条路径白名单）：
- 只认**成功步骤**产出的图（失败步骤没有产物）；
- 只认落在**本会话工作目录之下**且**真实存在**的文件——引用一张裂图比没有图更糟；
- 读侧白名单（:func:`is_safe_chart_name`）之外，写侧也必须消毒文件名
  （`viz_tool.safe_chart_name`）——LLM 给的 title 是任意串，不消毒会写到目录外。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

#: 报告里图片引用的 URL 前缀（绝对路径：报告在**运行期**由 chart 端点直出，相对路径此时必裂）。
CHART_URL_PREFIX = "/api/v1/chat/analyze/chart"

#: 报告**导出包**内图引用的目录前缀（相对路径：与包内 charts/*.png 同根，离线可看）。
CHART_URL_EXPORT_PREFIX = "charts"

#: 允许的图片扩展名。**不含 svg**：同源 inline 的 SVG 可带脚本，等于给自己开 XSS 面；
#: viz_tool 只产 PNG，放宽到常见位图格式已够。
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: 文件名白名单：字母数字、下划线、**中日韩汉字**（viz 的 title 常为中文）、点、连字符
_NAME_RE = re.compile(r"^[A-Za-z0-9_一-鿿.\-]+$")


def is_safe_chart_name(name: str) -> bool:
    """文件名是否在白名单内（第一层；第二层是 :func:`resolve_chart_file` 的目录校验）。

    两层都要：字符白名单挡掉**路径分隔符**（`/` `\\`，即一切穿越写法的必要成分），
    目录校验兜住白名单漏网；扩展名白名单把非位图挡在外面。

    **不额外禁止名字里的 `..`**：分隔符已被挡，`a..b.png` 落不到目录外；
    而写侧 `viz_tool.safe_chart_name` 允许这种名字——读侧更严 = 图会静默消失
    （见 `test_sanitized_names_always_pass_the_read_side_whitelist`）。
    """
    if not name or not isinstance(name, str):
        return False
    if len(name) > 120:
        return False
    if not _NAME_RE.match(name):
        return False
    return Path(name).suffix.lower() in IMAGE_EXTS


def media_type_for(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _workdir(session_id: str) -> Path:
    # 运行期导入：测试与部署都可能替换 session_workdir（export.py 同范式）
    from ...tools import session_workdir

    return Path(session_workdir(session_id)).resolve()


def resolve_chart_file(session_id: str, name: str) -> Optional[Path]:
    """把 ``name`` 解析成会话工作目录内真实路径；越界 → ``None``（不抛，交给调用方定 HTTP 码）。"""
    if not is_safe_chart_name(name):
        return None
    try:
        workdir = _workdir(session_id)
        resolved = (workdir / name).resolve()
        resolved.relative_to(workdir)      # 越界会抛 ValueError
    except Exception:
        return None
    return resolved


def collect_charts(state: Any) -> list[dict[str, Any]]:
    """本次运行可内嵌的图（有序、按文件名去重）。

    不满足条件的**静默剔除**——"没图"不是错误，是常态。
    """
    if state is None:
        return []
    try:
        workdir = _workdir(getattr(state, "session_id", "") or "")
    except Exception:
        return []

    found: dict[str, dict[str, Any]] = {}
    for r in getattr(state, "tool_results", None) or []:
        if getattr(r, "tool", "") != "visualization":
            continue
        if getattr(r, "status", "") != "SUCCESS":
            continue
        out = getattr(r, "output", None) or {}
        raw_path = out.get("image_path")
        if not raw_path:
            continue
        try:
            resolved = Path(str(raw_path)).resolve()
            resolved.relative_to(workdir)
        except Exception:
            continue
        if not resolved.is_file():
            continue
        name = resolved.name
        if not is_safe_chart_name(name):
            continue
        if name in found:                  # 同名图只嵌一次
            continue
        title = str((getattr(r, "input", None) or {}).get("title") or "").strip()
        found[name] = {
            "name": name,
            "title": title or resolved.stem,
            "chart_type": str(out.get("chart_type") or ""),
            "step_id": getattr(r, "step_id", ""),
            "path": str(resolved),
        }
    return list(found.values())


def _alt_text(title: str) -> str:
    """Markdown 图片 alt 里的 `]` 会提前闭合语法——最小转义。"""
    return str(title or "").replace("[", "(").replace("]", ")")


def embed_charts(report: str, charts: list[dict[str, Any]], session_id: str = "",
                 *, url_mode: str = "live") -> str:
    """报告末尾追加 ``## 图表``；无图则**原样返回**（绝不留一个空标题）。

    ``url_mode`` 控制图引用形态：
    - ``"live"``（默认）—— 绝对 URL ``{CHART_URL_PREFIX}/{session_id}/{name}``：
      运行期由 chart 端点直出；复制到导出包/剪贴板会裂，故**运行期唯一合法值**。
    - ``"export"`` —— 相对 URL ``charts/{name}``：与 ZIP 包内 ``charts/*.png`` 同根，
      离线打开 report.md 也能看图。仅 :func:`app.api.routes.export._build_items` 使用。
    """
    if not charts:
        return report or ""
    if url_mode == "export":
        prefix = CHART_URL_EXPORT_PREFIX
    else:
        prefix = f"{CHART_URL_PREFIX}/{session_id}"
    lines = ["", "## 图表", ""]
    for c in charts:
        url = f"{prefix}/{c['name']}"
        lines.append(f"![{_alt_text(c.get('title') or c['name'])}]({url})")
        lines.append("")
    return (report or "").rstrip() + "\n" + "\n".join(lines)


def strip_charts_section(report: str) -> str:
    """剥离末尾 ``## 图表`` 区段（如有），便于以另一 URL 模式重嵌。"""
    text = (report or "").rstrip()
    marker = "\n## 图表"
    idx = text.rfind(marker)
    return text[:idx].rstrip() if idx > 0 else text
