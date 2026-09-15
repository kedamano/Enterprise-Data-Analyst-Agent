"""``visualization`` tool – render a chart from a data spec and persist a PNG."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

try:  # matplotlib is optional at import time; degrade gracefully
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as _fm
    import matplotlib.pyplot as plt
    _HAVE_MPL = True
except Exception:  # pragma: no cover
    _HAVE_MPL = False


#: 中文字体候选（按优先级）。matplotlib 默认 sans-serif 首位是 DejaVu Sans，
#: **不含 CJK 字形**——中文标题会被画成一排空心方块（tofu）。
#: 2026-09-15 真模型跑 `test_visualization_runs` 时暴露：报告图内嵌（D51）刚上线，
#: 但中文标题的图实际是废图（`Glyph ... missing from font(s) DejaVu Sans`）。
_CJK_FONT_CANDIDATES = (
    "Microsoft YaHei",      # Windows
    "SimHei",               # Windows 备选
    "PingFang SC",          # macOS
    "Heiti SC",             # macOS 备选
    "Noto Sans CJK SC",     # Linux
    "Source Han Sans SC",   # Linux 思源
    "WenQuanYi Zen Hei",    # Linux 文泉驿
)


def _configure_cjk_font() -> str | None:
    """把本机**实际可用**的 CJK 字体插到 sans-serif 最前，返回选中的字体名。

    没有任何 CJK 字体时返回 None 并**保持原样**——不能因为选不到字体就让
    画图整个失败（ASCII 标题仍然是对的，坏一半好过全坏）。
    `axes.unicode_minus=False`：负号在无 CJK 字体时也会变方块。
    """
    if not _HAVE_MPL:  # pragma: no cover
        return None
    available = {f.name for f in _fm.fontManager.ttflist}
    chosen = next((n for n in _CJK_FONT_CANDIDATES if n in available), None)
    if chosen is None:  # pragma: no cover - 本机与 CI 均有中文字体
        return None
    # 插到最前而不是覆盖：DejaVu Sans 仍是 ASCII/数学符号的兜底
    matplotlib.rcParams["font.sans-serif"] = [chosen, *matplotlib.rcParams["font.sans-serif"]]
    matplotlib.rcParams["axes.unicode_minus"] = False
    return chosen


_CJK_FONT = _configure_cjk_font() if _HAVE_MPL else None


# Windows 保留字符 + 控制字符（macOS/Linux 也一并挡掉，产物要能跨平台搬运）
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: 空白（含全角空格）→ `_`：读侧白名单不含空白，留着会让图在端点里静默 400
_WHITESPACE = re.compile(r"\s+|　")
_MAX_NAME_LEN = 80

# Windows 设备名：`con.png` 这类名字连文件都建不出来（savefig 会莫名失败）
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def safe_chart_name(title: Any) -> str:
    """把 LLM 给的 ``title`` 变成安全文件名（**写侧**消毒）。

    `title` 来自 planner 参数或 ``ctx.objective[:30]``，即**任意串**。
    D51 之前直接 ``wd / f"{title}.png"``——title 含 ``../`` 时图会被写到会话目录
    **之外**；读侧白名单再严也堵不住写侧。规则：

    1. 只取最后一段路径（``a/b/c`` → ``c``，同时归一 Windows 的 ``\\``）；
    2. 替换非法字符、把空白折成 ``_``（读侧白名单不含空白）；
    3. 剥掉首尾空白与点（``..`` / ``.`` 这类"等于没起名"的值）；
    4. Windows 设备名前面加 ``_``（``con`` → ``_con``）；
    5. 空 → ``chart``（确定的兜底名，不留随机性）。

    产物必须能通过**读侧**白名单（`charts.is_safe_chart_name`）——
    否则图会静默消失，见 `test_sanitized_names_always_pass_the_read_side_whitelist`。
    """
    raw = str(title or "").replace("\\", "/").split("/")[-1]
    cleaned = _INVALID_NAME_CHARS.sub("_", raw)
    cleaned = _WHITESPACE.sub("_", cleaned).strip().strip(".").strip()
    cleaned = cleaned[:_MAX_NAME_LEN]
    if cleaned.lower() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned or "chart"



def run(params: dict[str, Any], workdir: str | None = None) -> dict[str, Any]:
    if not _HAVE_MPL:
        return {"ok": False, "error": "matplotlib 未安装，无法生成图表"}
    data = params.get("data") or []
    csv_path = params.get("csv_path")
    if csv_path and not data:
        import pandas as pd
        data = pd.read_csv(csv_path).to_dict(orient="records")
    if not data:
        return {"ok": False, "error": "缺少 data 或 csv_path"}
    x = params.get("x")
    y = params.get("y")
    chart_type = (params.get("chart_type") or "bar").lower()
    title = params.get("title", "chart")

    try:
        xs = [r.get(x) for r in data]
        ys = [r.get(y) for r in data]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        if chart_type == "line":
            ax.plot(xs, ys, marker="o")
        elif chart_type == "pie":
            ax.pie(ys, labels=xs, autopct="%1.1f%%")
        else:
            ax.bar([str(v) for v in xs], ys)
        ax.set_title(title)
        ax.set_xlabel(x or "")
        ax.set_ylabel(y or "")
        fig.tight_layout()
        wd = Path(workdir or tempfile.mkdtemp(prefix="da_viz_"))
        wd.mkdir(parents=True, exist_ok=True)
        # D51：文件名必须消毒——`title` 是 LLM 给的任意串，不消毒会写出会话目录
        out_path = wd / f"{safe_chart_name(title)}.png"
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "image_path": str(out_path), "chart_type": chart_type, "points": len(data)}
