"""``visualization`` tool – render a chart from a data spec and persist a PNG."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:  # matplotlib is optional at import time; degrade gracefully
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _HAVE_MPL = True
except Exception:  # pragma: no cover
    _HAVE_MPL = False


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
        out_path = wd / f"{title or 'chart'}.png"
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "image_path": str(out_path), "chart_type": chart_type, "points": len(data)}
