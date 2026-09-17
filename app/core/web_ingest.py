"""网页正文抓取 —— 供「网站知识库」导入使用。

用户在第一张参考图里看到的「网站知识库」类型，语义是：**把一个站点的页面
抓下来、抽成正文、切块入 RAG**，而不是只存一个 URL 链接。因此这里做的是
「抓取 + 正文提取」，把 HTML 里的导航/脚本/样式全部剥掉，只留可检索的正文。

失败一律返回 ``{"ok": False, "error": ...}`` 而**不抛异常**：导入是用户发起的
交互动作，前端需要拿一句话解释为什么失败（超时 / 非 HTML / 拒绝了），
抛 500 只会给一个无信息量的报错页。
"""
from __future__ import annotations

import re
from typing import Any

TIMEOUT_S = 20.0
# 正文上限：单页超过这个量级就不是"知识条目"而是数据倾倒了，截断保护上下文预算
MAX_CHARS = 200_000
_UA = "Mozilla/5.0 (compatible; EnterpriseDataAnalyst/1.0; +local)"

# 这些标签整体丢弃：它们只承载导航/装饰/行为，正文价值为零且会稀释检索
_DROP_TAGS = (
    "script", "style", "noscript", "template", "svg", "iframe", "canvas",
    "nav", "footer", "header", "aside", "form", "button", "select",
)


def _extract(html: str, url: str) -> tuple[str, str]:
    """返回 ``(title, 正文文本)``。解析器不可用时退回正则粗提取。"""
    try:
        from bs4 import BeautifulSoup

        for parser in ("lxml", "html.parser"):
            try:
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                soup = None
        if soup is None:
            raise RuntimeError("no parser")
        for tag in soup(_DROP_TAGS):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        body = soup.body or soup
        text = body.get_text("\n", strip=True)
    except Exception:
        # 无 bs4 / 解析器缺失：剥标签的粗提取，聊胜于无（比整页 HTML 入库好）
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = (title_m.group(1).strip() if title_m else "")
        text = re.sub(r"<[^>]+>", "\n", html)

    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return title or url, text


def fetch_website(url: str, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """抓取 *url* 并提取正文。

    返回 ``{ok, url, title, text, bytes}``；失败时 ``{ok: False, error}``。
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "url": url, "error": "链接为空"}
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "url": url, "error": "仅支持 http:// 或 https:// 链接"}

    html = ""
    err = ""
    try:
        import httpx

        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.get(url, headers={"User-Agent": _UA})
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:  # 网络/证书/超时/4xx-5xx 统一降级成一句人话
        err = f"{type(exc).__name__}: {exc}"

    if not html:
        # httpx 不可用或失败 → 用标准库兜底（内网 http 站点常能过）
        try:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read()
            html = raw.decode("utf-8", errors="replace")
            err = ""
        except Exception as exc:
            return {"ok": False, "url": url, "error": err or f"{type(exc).__name__}: {exc}"}

    title, text = _extract(html, url)
    if not text.strip():
        return {"ok": False, "url": url, "error": "页面未提取到正文文本（可能是纯 JS 渲染）"}
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]
    return {
        "ok": True,
        "url": url,
        "title": title,
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "truncated": truncated,
    }
