"""RAG 检索的置信度判据（RAG-01）。

Spec: ``docs/specs/RAG/01-low-confidence-fallback.md``

**断在哪**：``knowledge_search`` 无论检索到什么都照单返回——"检索到 0 段"与
"检索到 4 段无关内容"在调用方看来一模一样，模型也就把无关段落当**知识**写进结论。

**判据按检索后端分两条（互斥，绝不双算——否则"拨一下 CE 开关判级就变"，踩 D41/D47 老坑）**：
- phrase-affinity（默认）：``reranker._phrase_affinity(query, 首条.text)``，阈值
  ``rag_min_confidence``（**标定** ``0.15``，§2.1.1）。
- cross_encoder（CE 启用时）：首条 chunk 的 ``rerank_score`` 来自 ``CrossEncoder.predict``，
  尺度与亲和分完全不同，**独立阈值** ``rag_cross_encoder_confidence_threshold``（默认 0.5）。
  选用 CE 判据的动机（spec §4）：字面重合但语义无关的段落（FAQ 式的"问句复述"首条）用亲和分
  压到 ≈1.0 的高位；只有 CE 才能把它压回 low。

为什么不拿 deterministic 的 ``rerank_score`` 判级（它是 `0.8×亲和 + 0.2×RRF归一`，看起来更"专业"）：
归一化项有个**地板分**——完全无关的首条只要排在第一，也能拿到 `0.2×1.0 = 0.2`。
实测黄金集（spec §2.1.1）：负例在重排开启时首条恰为 `0.200`，而重排关闭时同一批正例
最低只有 `0.188`。**同一个阈值不可能同时成立**，于是"拨一下 RERANK_ENABLED，
同一次检索从 high 变 low"——**这正是 D41/D47 踩过的坑**。CE 走不同模型输出，
不存在 RRF 地板分问题，才适合当独立判据。
``rerank_score`` 仍在 chunk 里透出（CE 与亲和两路都写），只是**deterministic 路不当判据**。

为什么取**首条**而不是均值：RAG 的失败模式是"首条就不相关"，后面几条更不相关；
取平均会让一条高分的正确段落把一个无关段落抬过阈值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ...config import get_settings
from .reranker import _cross_encoder_available, _phrase_affinity

#: 配置缺失/非法时的兜底阈值（与 ``Settings.rag_min_confidence`` 默认值一致）
DEFAULT_THRESHOLD = 0.15
#: CE 模式兜底阈值（与 ``Settings.rag_cross_encoder_confidence_threshold`` 一致）
DEFAULT_CE_THRESHOLD = 0.5

# D62 字面回声检测 -------------------------------------------------------------
#: 首条 = 问题的字面复述 → 判 low。把句末疑问词/标点 / FAQ 头剥掉再比大小。
_TAIL_INTERROG = re.compile(r"[吗呢？?!！\s]+$")
_PREFIX_NOISE = re.compile(r"^(用户问[:：]\s*|Q[:：]\s*)")


def _echo_strip(s: str) -> str:
    """把"问句噪声"剥掉：前后空白 + 句末疑问词/标点 + FAQ 头（"用户问：" / "Q:"）。"""
    s = s.strip()
    s = _TAIL_INTERROG.sub("", s)
    s = _PREFIX_NOISE.sub("", s)
    return s.strip()


def _echo_ratio(query: str, text: str) -> float:
    """把两端噪声剥掉后 ``len(text)/len(query)``；任一端为空 → 0.0。

    只有 ≥1.0 的才有意义（text 比 query 长或等长）。
    """
    q = _echo_strip(query)
    t = _echo_strip(text)
    if not q or not t:
        return 0.0
    return len(t) / len(q)


def _is_echo_chunk(query: str, text: str) -> bool:
    """首条是否"只是把问题重抄了一遍"。

    判据：query 在剥掉句末疑问词/标点与 FAQ 头之后，长度 ≥ ``rag_echo_min_chars``，
    且首条在同一剥法下的长度是 query 的 1.0 ~ ``rag_echo_ratio_max`` 倍。

    直觉：chunk 只是 query 的复述（≈ 同尺寸、没多信息），不是带答案的文档（ratio 越大
    答案越多）。
    """
    settings = get_settings()
    if not settings.rag_echo_demotion_enabled:
        return False
    q = _echo_strip(query)
    if len(q) < settings.rag_echo_min_chars:
        return False
    ratio = _echo_ratio(query, text)
    return 1.0 <= ratio <= settings.rag_echo_ratio_max


@dataclass(frozen=True)
class Confidence:
    """一次检索的可信度。``score`` 是判级用的原始分（阈值不修改它）。"""

    level: str   # "high" | "low" | "none"
    score: float
    basis: str   # "phrase_affinity" | "cross_encoder" | "no_chunks" | "no_signal" | "echo_question"


def threshold() -> float:
    """当前阈值；配置非法时退回默认，**绝不抛**（配置写错不该让检索整体不可用）。"""
    try:
        value = float(getattr(get_settings(), "rag_min_confidence", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):  # pragma: no cover - 校验器已挡住，双保险
        return DEFAULT_THRESHOLD
    return DEFAULT_THRESHOLD if value < 0 else value


def ce_threshold() -> float:
    """CE 模式阈值；配置非法时退回默认，**绝不抛**（配置写错不该让检索整体不可用）。"""
    try:
        value = float(getattr(get_settings(),
                              "rag_cross_encoder_confidence_threshold", DEFAULT_CE_THRESHOLD))
    except (TypeError, ValueError):  # pragma: no cover - 校验器已挡住，双保险
        return DEFAULT_CE_THRESHOLD
    return DEFAULT_CE_THRESHOLD if value < 0 else value


def confidence_of(query: str, chunks: list[dict[str, Any]] | None) -> Confidence:
    """判断这次检索值不值得信。

    fail-closed：**算不出信号时按 `low` 处理**，绝不因为"没算出问题"就默认高置信——
    与脱敏"出错必须丢掉行样本"是同一条纪律。

    判据路径（两条，互斥）：
    - 默认：取首条文本的 ``_phrase_affinity``（与 rerank_score 解耦，见同文件顶部说明）。
    - CE 启用：cross-encoder 可用时，改取首条 ``rerank_score`` 作为判据
      （``basis="cross_encoder"``，独立阈值 ``settings.rag_cross_encoder_confidence_threshold``）。
      标 ``[RAG-01 §4]``——字面重合但语义无关的段落只有 CE 分能压得下。
    """
    if not chunks:
        return Confidence("none", 0.0, "no_chunks")
    text = str(chunks[0].get("text") or "").strip()
    if not text:
        # 有候选却无从判读（无文本）→ 不能假装它可信
        return Confidence("low", 0.0, "no_signal")
    q = query or ""
    score_aff = round(_phrase_affinity(q, text), 4)   # echo 检测与 CE 失败兜底共用
    # D62：首条只是 query 的字面复述 → 形高实低，强制降到 low（而不看亲和分）。
    # 无论 CE 是否启用都生效——echo 不是噪声，**是"把问题抄了一遍的段落"**，不能当知识用。
    if _is_echo_chunk(q, text):
        return Confidence("low", score_aff, "echo_question")
    # --- CE 启用：两种判据选一种，绝不双算（同一件事两个定义 = D41/D47 的老坑） ---
    if _cross_encoder_available():
        ce_score = chunks[0].get("rerank_score")
        if ce_score is None:
            # CE 排序完竟没写 rerank_score → 视为信号缺失，fail-closed 退 low（不猜）
            return Confidence("low", score_aff, "no_signal")
        level = "high" if float(ce_score) >= ce_threshold() else "low"
        return Confidence(level, round(float(ce_score), 4), "cross_encoder")
    # --- 默认路径（CE 未启用）：phrase_affinity 这一个尺度 ---
    score = score_aff
    return Confidence("high" if score >= threshold() else "low", score, "phrase_affinity")
