"""E9/02 Query 改写 — 检索前的规则化改写层。

目标：缩小 query 与企业知识库文档之间的 **vocabulary gap**：

* 归一化（全角 ASCII → 半角、括号 / 空白归一）；
* 口语填充词剥离（「帮我查一下」「请问」「呢」「吗」等前后缀）；
* 可选的「行业词表」同义词映射 → 产出原 query 之外的若干 **alternative phrasing**，
  注入 D60 多跳的 fan-out（原 query + 各 alternative 各召回一次、合并去重、
  按原 query rerank）。

设计要点：
* **LLM-free**：改写只做确定性文本变换，不需要 LLM 调用（检索路径上不打墙钟）。
* **fail-open**：`rewrite()` 内部任何异常 → 返回原 query 未改写的 ``RewriteResult``；
  ``retrieve_many`` 单 seed 异常 → 跳过，不影响其它 seed。
* **行业词表**（`rag_query_rewrite_synonym_path`）：文件 UTF-8，
  每行 ``k = v`` 或 ``k,v``（首字符 = / , 即视为分隔符，前后空白 strip）；
  ``#`` 开头的行为注释。语法 / 编码错误词表 → 静默空词典（宁可不改也不拖挂检索）。
* 主改写**不**替换 synonym；synonym 只在 ``alternatives`` 里注入替换 phrasing——
  原 query 的语义仍以 fan-out 形式参与召回，不因 synonym 映射损失多样性。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import get_settings

# --------------------------------------------------------------------------- #
# 可观测：模块级计数（与 multihop._METRICS 模式一致）
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_METRICS: dict[str, float] = {
    "rag_query_rewrite_total": 0.0,          # 改写调用次数
    "rag_query_rewrite_synonym_hits_total": 0.0,  # 同义词命中总次数
    "rag_query_rewrite_fallback_total": 0.0,       # 改写失败退回次数
}


def rewrite_metrics() -> dict[str, float]:
    """返回改写层计数快照（用于测试 + /metrics 暴露）。"""
    with _lock:
        return dict(_METRICS)


def _emit(name: str, amount: float = 1.0) -> None:
    with _lock:
        _METRICS[name] += amount


# --------------------------------------------------------------------------- #
# 口语剔除 + 归一化
# --------------------------------------------------------------------------- #

# 口语填充词白名单（前后缀各出现即剔除）。长度从长到短尝试，避免把「帮我查一下」
# 里的「帮我」先吃掉留下「查一下」——白名单按长度降序排列即可。
_CONVERSATIONAL: tuple[str, ...] = tuple(sorted((
    "帮我查一下", "帮我查", "查一下",
    "我想知道", "我想问一下", "问一下",
    "我想了解", "了解一下",
    "请教一下", "请教",
    "麻烦您", "麻烦你", "麻烦",
    "帮我看一下", "看一下",
    "帮我看看", "看一下",
    "帮帮忙", "帮帮我",
    "请问", "麻烦", "请",
    "帮我", "给我", "求",
), key=len, reverse=True))

# 纯口语收尾语气/助词（在主词剥离后尾随的零碎）。
_TAIL_PARTICLES = ("呢", "吗", "啊", "吧", "呀", "么", "嘛", "啦")


def _to_halfwidth(s: str) -> str:
    """全角 ASCII 字符 (U+FF01..U+FF5E) → 半角 (U+0021..U+007E)；全角空格 → 普通空格。

    中文标点保持不动（全角逗号、括号等中文语境下留着无妨；代码中的全角括号我们另外处理）。
    """
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:                   # 全角空格
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:       # 全角 ASCII 区
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


# 全角括号 + 中文括号 → 空白（避免「（营收）」营收 粘成 「营收」 不带空格 → BM25 CJK bigram
# 把括号切出的词粘起来会少交叉匹配）。
_PAREN_RE = re.compile(r"[()（）\[\]【】{}｛｝]+")

# 收尾多余的空白 / 标点
_WS_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    s = _to_halfwidth(s)
    s = _PAREN_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _strip_conversational(s: str) -> str:
    """剥口语前后缀。循环至多 N 次防止死循环，直到稳定。"""
    prev = None
    n = 0
    while prev != s and n < 8:
        prev = s
        # 前缀
        for word in _CONVERSATIONAL:
            if s.startswith(word):
                s = s[len(word):]
                break
        # 后缀
        for word in _CONVERSATIONAL:
            if s.endswith(word):
                s = s[:-len(word)]
                break
        # 尾随语气词（单字，主词剥完后零碎收尾）
        for p in _TAIL_PARTICLES:
            if s.endswith(p):
                s = s[:-len(p)]
                break
        s = s.strip()
        n += 1
    return s


# --------------------------------------------------------------------------- #
# 行业词表
# --------------------------------------------------------------------------- #

_COMMENT_RE = re.compile(r"^\s*#")
# 非空行、非注释行的分隔；第一个出现 `=` 或 `,` 处切分。
_SEP_RE = re.compile(r"[=,]")


def _load_synonyms(path: str) -> dict[str, str]:
    """读行业词表 → {k: v}（均 strip）。

    * 文件不存在 / 不可读 / 编码错 → {} (fail-open)
    * 行内无分隔符 / k 或 v 为空 → 跳过该行
    """
    if not path:
        return {}
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return {}
    except (OSError, ValueError):
        return {}

    out: dict[str, str] = {}
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or _COMMENT_RE.match(line):
                continue
            m = _SEP_RE.search(line)
            if not m:
                continue
            k = line[:m.start()].strip()
            v = line[m.end():].strip()
            if k and v:
                out[k] = v
    except (OSError, UnicodeDecodeError):
        return {}
    return out


# --------------------------------------------------------------------------- #
# QueryRewriter
# --------------------------------------------------------------------------- #


@dataclass
class RewriteResult:
    original: str              # 入参原 query
    rewritten: str             # 主改写结果（直接替换原 query 送检索）
    alternatives: list[str]    # 改写变体 / 同义 phrasing（注入 fan-out）
    changed: bool              # 是否与原 query 不同
    mode: str                  # "rule"（当前实现；LLM 模式留作后续卡）
    synonym_hits: list[str]    # 命中的同义词键（metrics / trace 用）


class QueryRewriter:
    """规则化 query 改写器（LLM 模式留口，当前只实现 rule 模式）。"""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        synonym_path: str | None = None,
        max_alternatives: int | None = None,
        min_len: int | None = None,
    ) -> None:
        st = get_settings()
        self._enabled: bool = bool(enabled) if enabled is not None \
            else getattr(st, "rag_query_rewrite_enabled", True)
        _syn_path = synonym_path if synonym_path is not None \
            else getattr(st, "rag_query_rewrite_synonym_path", "")
        self._max_alt: int = max_alternatives if max_alternatives is not None \
            else getattr(st, "rag_query_rewrite_max_alternatives", 2)
        self._min_len: int = min_len if min_len is not None \
            else getattr(st, "rag_query_rewrite_min_len", 4)
        # 懒加载 + 缓存：synonym 文件内容不变则不反复读盘
        self._syn_path: str = _syn_path
        self._synonyms: dict[str, str] | None = None
        self._synonyms_loaded_for: str = ""

    # -- public --------------------------------------------------------- #
    def rewrite(self, query: str) -> RewriteResult:
        """确定性改写；任何异常 → 返回原 query 未改写的 RewriteResult（fail-open）。"""
        if not self._enabled:
            return RewriteResult(original=query, rewritten=query, alternatives=[],
                                 changed=False, mode="rule", synonym_hits=[])
        try:
            return self._do_rewrite(query)
        except Exception:
            _emit("rag_query_rewrite_fallback_total")
            return RewriteResult(original=query, rewritten=query, alternatives=[],
                                 changed=False, mode="rule", synonym_hits=[])

    # -- internal ------------------------------------------------------- #
    def _ensure_synonyms(self) -> dict[str, str]:
        if self._syn_path != self._synonyms_loaded_for or self._synonyms is None:
            self._synonyms = _load_synonyms(self._syn_path)
            self._synonyms_loaded_for = self._syn_path
        return self._synonyms

    def _do_rewrite(self, query: str) -> RewriteResult:
        if not (query or "").strip():
            _emit("rag_query_rewrite_total")
            return RewriteResult(original=query, rewritten=query, alternatives=[],
                                 changed=False, mode="rule", synonym_hits=[])

        # 1. 归一化（全半角、括号、空白）
        normalized = _normalize(query)
        # 2. 口语剥离
        stripped = _strip_conversational(normalized)
        # 3. synonym → alternatives（主改写不替换）
        synonyms = self._ensure_synonyms()
        alternatives: list[str] = []
        synonym_hits: list[str] = []
        if synonyms and stripped:
            for k, v in synonyms.items():
                # 精确包含子串即视为命中；大小写敏感（企业术语区分大小写是合理的）
                if k in stripped:
                    alt = stripped.replace(k, v)
                    if alt and alt != stripped and alt not in alternatives:
                        alternatives.append(alt)
                        synonym_hits.append(k)
                        if len(alternatives) >= self._max_alt:
                            break

        # 主改写 = 归一化 + 口语剥离（不替换 synonym）
        rewritten = stripped if stripped else normalized

        # 长度兜底：改写后过短 → 退回原 query（宁可不改）
        if len(rewritten) < self._min_len:
            _emit("rag_query_rewrite_total")
            _emit("rag_query_rewrite_fallback_total")
            return RewriteResult(original=query, rewritten=query, alternatives=[],
                                 changed=False, mode="rule", synonym_hits=[])

        changed = (rewritten != query) or bool(alternatives)
        _emit("rag_query_rewrite_total")
        if alternatives:
            _emit("rag_query_rewrite_synonym_hits_total", len(alternatives))
        return RewriteResult(
            original=query, rewritten=rewritten,
            alternatives=alternatives, changed=changed, mode="rule",
            synonym_hits=synonym_hits,
        )


# --------------------------------------------------------------------------- #
# 模块级默认实例（懒初始化，便于 run() 内部直接引用）
# --------------------------------------------------------------------------- #
_default_rewriter: QueryRewriter | None = None
_default_rewriter_lock = threading.Lock()


def get_rewriter() -> QueryRewriter:
    """返回默认 QueryRewriter（与当前 settings 绑定；settings 单例+frozen 在测试可 patch）"""
    global _default_rewriter
    with _default_rewriter_lock:
        if _default_rewriter is None:
            _default_rewriter = QueryRewriter()
        return _default_rewriter


def reset_default_rewriter() -> None:
    """测试夹具用：清空 default 单例，重建。"""
    global _default_rewriter
    with _default_rewriter_lock:
        if _default_rewriter is not None:
            _METRICS["rag_query_rewrite_total"] = 0.0
            _METRICS["rag_query_rewrite_synonym_hits_total"] = 0.0
            _METRICS["rag_query_rewrite_fallback_total"] = 0.0
        _default_rewriter = None


def reset_rewrite_metrics() -> None:
    """仅把模块级 metrics 归零（不触动 _default_rewriter 单例）。

    给 conftest ``_reset_state`` 用：每个测试开始计数从 0 起，避免跨测试污染。
    不改单实例是因为 settings 缓存已经被 ``get_settings.cache_clear()`` 清了，
    缓存着也不影响下一个测试的 patch 流。
    """
    with _lock:
        _METRICS["rag_query_rewrite_total"] = 0.0
        _METRICS["rag_query_rewrite_synonym_hits_total"] = 0.0
        _METRICS["rag_query_rewrite_fallback_total"] = 0.0
