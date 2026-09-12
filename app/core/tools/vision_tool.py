"""P2-2：图片视觉解析工具。

根据用户上传的图片（已落地到 ``data/uploads/<session>/images/``）和提问，
调用视觉模型识别图中的图表 / 数据 / 文字，返回结构化描述，作为后续分析的证据。

工具本身的注册、参数解析、附件定位、错误处理都是**确定性**的，可用
``MOCK_LLM`` 离线验证；只有「图片内容识别」这一步依赖视觉 LLM。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("da.tools.vision")


def _resolve_image_path(image: str, session_id: str) -> str | None:
    """按文件名在 session 附件里定位已落地的图片绝对路径。"""
    from ..attachments import attached_images, image_dir, _safe_filename

    sid = session_id or ""
    # 1) 直接命中已注册附件（path 已知，最可靠）
    for img in attached_images(sid):
        if img.get("name") == image or img.get("path") == image:
            if img.get("path") and Path(img["path"]).exists():
                return img["path"]
    # 2) 兜底：到图片落地目录按文件名 / 安全文件名匹配（兼容只传了文件名）
    d = image_dir(sid)
    for c in (d / image, d / _safe_filename(image)):
        if c.exists():
            return str(c)
    return None


def _first_param(params: dict[str, Any], *names: str) -> str:
    """按别名依次取第一个非空字符串参数。

    真实 LLM 不保证用我们文档里的参数名：它可能写 ``image_path`` / ``path`` /
    ``file`` / ``image_file`` 而不是 ``image``。硬要 ``image`` 会以
    「缺少参数 image」把整个 image_analyze 步骤判死（真实 e2e 实测过）。
    """
    for name in names:
        val = params.get(name)
        if isinstance(val, str) and val.strip():
            return val.strip()
        # 也接受 {"image": {"path": "..."}} 这种嵌套写法
        if isinstance(val, dict):
            for sub in ("path", "file", "filename", "name"):
                inner = val.get(sub)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return ""


# 常见图片扩展名 —— 用于从自然语言里"捞"文件名
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")


def _guess_image_from_text(*texts: str) -> str:
    """从自由文本里猜出图片文件名。

    真实 e2e 暴露的更深一层问题：模型（gemma）**根本不填 ``input``**，
    而是把参数写进人话 objective —— 例如：

        objective = "Read and describe the content and key values of sales_chart.png"

    此时 ``input`` 是 ``null``，任何"参数别名表"都无济于事。这里做一次
    最后兜底：用正则从 objective / question 里捞出带图片扩展名的 token。
    """
    import re

    pattern = r"[A-Za-z0-9_\-\u4e00-\u9fff]+(?:\.[A-Za-z0-9_\-]+)*?(?:%s)" % "|".join(
        re.escape(e) for e in _IMAGE_EXTS
    )
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        # 优先带引号的完整文件名
        for quoted in re.findall(r"[\"'`]([^\"'`]+?)[\"'`]", text):
            if quoted.lower().endswith(_IMAGE_EXTS):
                return quoted.strip()
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return ""


def run(params: dict[str, Any], workdir: str | None = None) -> dict[str, Any]:
    image = _first_param(params, "image", "image_path", "path", "file", "image_file",
                         "filename", "image_name")
    question = _first_param(params, "question", "query", "prompt", "instruction", "ask")
    session_id = params.get("_session_id", "") or ""

    # ---- 兜底一层：模型没填 input，把文件名写进了自然语言（objective/action） ----
    # 真实 e2e：gemma 给出 objective="Read ... sales_chart.png"，input=null。
    # nodes.build_executor_params 已尝试回填；这里再兜一次，保证工具自身健壮。
    if not image:
        image = _guess_image_from_text(
            str(params.get("_objective") or ""),
            str(params.get("_action") or ""),
            str(params.get("objective") or ""),
            str(params.get("action") or ""),
            question,
        )
    # ---- 兜底二层：文本里也没有文件名 → 若 session 下只有一张图，直接用 ----
    if not image:
        try:
            from ..attachments import attached_images
            imgs = attached_images(session_id)
            if len(imgs) == 1:
                image = imgs[0].get("name") or imgs[0].get("path") or ""
        except Exception:
            pass

    # 模型有时只给绝对路径、不给文件名 —— 也能直接寻址（限本 session 的落地图）
    if image and not image.startswith("data:"):
        from pathlib import Path as _P
        candidate = _P(image)
        if candidate.is_absolute() and candidate.exists():
            # 安全：只允许 session 图片目录内的文件
            from ..attachments import image_dir
            try:
                allowed = image_dir(session_id).resolve()
                if candidate.resolve().parent == allowed:
                    return _analyze(str(candidate), image, question)
            except Exception:
                pass

    if not image:
        return {
            "ok": False,
            "error": "缺少参数 image（上传图片的文件名）",
            "error_class": "NON_RETRYABLE",
        }

    path = _resolve_image_path(image, session_id)
    if not path:
        return {
            "ok": False,
            "error": (
                f"未找到图片附件：{image}（session={session_id}）。"
                "请先通过 /attachments/upload 上传图片。"
            ),
            "error_class": "NON_RETRYABLE",
        }
    return _analyze(path, image, question)


def _analyze(path: str, image: str, question: str) -> dict[str, Any]:
    system = (
        "你是企业数据分析助手。用户上传了一张图片（可能是业务图表、仪表盘截图、"
        "表格照片或包含数据的截图）。请从图片中提取**可用于分析的结构化信息**："
        "关键数值、指标、维度、趋势、图表类型、坐标轴含义、可见的文字标签。"
        "只基于图片内容回答，不要编造图片中不存在的数据；若图片信息不足，"
        "明确说明无法识别的部分。"
    )
    user = question or "请提取这张图片中的关键数据、指标和趋势，给出结构化描述。"

    try:
        from ...infrastructure.llm.router import get_llm
        raw = get_llm().vision(system, user, [path], stage="vision", json_mode=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"视觉模型调用失败：{exc}",
            "error_class": "RETRYABLE",
        }

    description = _extract_description(raw)
    return {
        "ok": True,
        "image": image,
        "question": question,
        "description": description,
        # json_mode 返回已是结构化 JSON，description 已抽取为可读文本，
        # 不再重复塞整段 JSON 进上下文（避免撑爆 token 预算）。
        "artifacts": [path],
    }


def _extract_description(raw: str) -> str:
    """从视觉模型输出里抽取可读描述；兼容 JSON（json_mode）与纯文本。"""
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return raw
        if isinstance(obj, dict):
            if obj.get("description"):
                return obj["description"]
            if obj.get("text"):
                return obj["text"]
            # 无显式描述字段 → 把结构化字段拼成可读文本，保留数值证据
            parts: list[str] = []
            for k, v in obj.items():
                if k == "structured_data" and isinstance(v, list):
                    for row in v:
                        parts.append(" | ".join(f"{kk}={vv}" for kk, vv in row.items()))
                else:
                    parts.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
            return "\n".join(parts)
    return raw
