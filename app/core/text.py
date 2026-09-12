"""共享文本工具：轻量分词（中文 2-gram + 英文/数字按词）。

被「记忆三因子打分」与「工具路由」共用——两处都需要**确定性、零依赖**的中文分词，
各写一份必然漂移。刻意不引入 jieba 之类：离线可跑、结果可复现是本项目的硬约束。
"""
from __future__ import annotations

import re

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str, *, keep_single_cjk: bool = True) -> set[str]:
    """返回词元集合：英文/数字按词、中文按 2-gram（可选保留单字）。"""
    t = str(text or "").lower()
    tokens = set(_WORD_RE.findall(t))
    cjk = _CJK_RE.findall(t)
    tokens.update(f"{cjk[i]}{cjk[i + 1]}" for i in range(len(cjk) - 1))
    if keep_single_cjk:
        tokens.update(cjk)  # 短查询也留单字，避免零命中
    return {x for x in tokens if x}
