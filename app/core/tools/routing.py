"""INTERVIEW/01 ③ 工具路由：工具变多时按语义选工具。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §3（八股文 04.4）

现状：8 个工具全量塞给 planner，够用但**不可扩展**——工具涨到 100 个时，
prompt 会被工具说明淹没，模型选错率上升、token 成本线性膨胀。

这里用 **TF-IDF 向量 + 余弦相似度**做路由——这就是"基于向量检索的工具路由"的
最小正确实现：确定性、零依赖、离线可跑（不需要 embedding 服务）。
工具文档 = `name + description + input_schema 属性名`（属性名常含中文别名，判别力强）。

**自适应**：工具数 ≤ `tool_routing_threshold` 时全给（小工具集路由是负收益）；
超过才只给 top-k。**命中不足时回退全量**——绝不能因为路由失误让 planner 无工具可用。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Optional

from ..text import tokenize

DEFAULT_TOP_K = 6
DEFAULT_THRESHOLD = 0.02

# 中文别名表：**工具自身描述是英文的**（`Search available enterprise datasets…`），
# 而分析师说的是中文——只靠英文描述，中文查询会零命中并触发"回退全量"，
# 路由就白做了。企业部署里这份别名应随工具注册一起维护（此处给出可用的内置版）。
_ALIASES: dict[str, str] = {
    "schema_search": "有哪些表 表结构 字段 列名 元数据 数据源 库表 schema",
    "knowledge_search": "业务知识 口径 定义 指标含义 规则 术语 知识库 文档 检索 口径说明",
    "dataset_profile": "数据质量 画像 主键 唯一性 缺失 空值 粒度 日期连续性 join 放大 重复",
    "sql_query": "SQL 查询 取数 聚合 分组 统计 汇总 明细 排行",
    "freeform": "自定义SQL 手写查询 复杂查询 多表 关联 join 窗口函数 子查询",
    "python_analysis": "Python 脚本 代码 计算 清洗 处理 CSV Excel 建模 回归 统计检验 显著性",
    "visualization": "图表 画图 可视化 柱状图 折线图 趋势图 饼图 散点图",
    # E2/05：这份别名对 planner **已失效**——`generate_report` 在
    # `select_tools_for_planner` 里于**计数与打分之前**就被摘掉（见 NOT_PLANNABLE_TOOLS），
    # 它既不进候选、也占不到 top-k 名额。保留原文是因为 `route_tools` 本身仍是通用原语
    # （直接调用它的人按语义拿工具，不受 planner 的禁令约束）。
    "generate_report": "报告 周报 月报 汇报 结论 建议 总结",
    "image_analyze": "图片 截图 看图 图表识别 OCR",
}


def tool_document(name: str, spec: Any = None) -> str:
    """把工具规格拼成一段可检索文本（名称 + 描述 + 参数名 + 权限域 + **中文别名**）。"""
    parts = [name, name.replace("_", " "), _ALIASES.get(name, "")]
    if spec is not None:
        parts.append(str(getattr(spec, "description", "") or ""))
        schema = getattr(spec, "input_schema", None) or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(props, dict):
            parts.extend(str(k) for k in props)
        parts.append(str(getattr(spec, "data_scope", "") or ""))
    return " ".join(p for p in parts if p)


def _vectors(docs: dict[str, str]) -> dict[str, dict[str, float]]:
    """TF-IDF 向量化（idf 用 log((N+1)/(df+1))+1，平滑避免除零）。

    **只用 2-gram，不保留中文单字**：工具文档足够长，单字会制造假命中——
    实测「业务知识」曾因「识别」共享一个「识」字被路由到 `image_analyze`。
    """
    tokenized = {name: tokenize(doc, keep_single_cjk=False) for name, doc in docs.items()}
    n = max(1, len(tokenized))
    df: dict[str, int] = {}
    for toks in tokenized.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}
    out: dict[str, dict[str, float]] = {}
    for name, toks in tokenized.items():
        vec: dict[str, float] = {}
        for t in toks:
            vec[t] = (1.0 + math.log(1.0)) * idf.get(t, 1.0)  # tf=1（工具文档极短）
        out[name] = vec
    return out


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    num = sum(a[t] * b[t] for t in common)
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def _query_vector(query: str, idf: dict[str, float]) -> dict[str, float]:
    return {t: idf.get(t, 1.0) for t in tokenize(query, keep_single_cjk=False)}


def route_tools(query: str, *, top_k: int = DEFAULT_TOP_K,
                threshold: float = DEFAULT_THRESHOLD,
                specs: Optional[dict[str, Any]] = None,
                names: Optional[Iterable[str]] = None) -> list[str]:
    """按语义相关度返回工具名（降序）。**全部低于阈值时回退全量**（宁可多给）。"""
    if specs is None:
        from .specs import TOOL_SPECS

        specs = TOOL_SPECS
    all_names = list(names if names is not None else specs.keys())
    if not all_names:
        return []

    docs = {n: tool_document(n, specs.get(n)) for n in all_names}
    vecs = _vectors(docs)

    # 查询向量用同一份 idf（同样只用 2-gram，保持与文档侧一致的判别粒度）
    tokenized = {n: tokenize(d, keep_single_cjk=False) for n, d in docs.items()}
    n_docs = max(1, len(tokenized))
    df: dict[str, int] = {}
    for toks in tokenized.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n_docs + 1) / (d + 1)) + 1.0 for t, d in df.items()}
    q_vec = _query_vector(query, idf)

    scored = sorted(((_cosine(q_vec, vecs[n]), n) for n in all_names),
                    key=lambda p: (-p[0], p[1]))  # 分数降序 + 名称升序（确定性）
    hits = [n for score, n in scored if score >= threshold][:top_k]
    return hits or sorted(all_names)


def select_tools_for_planner(query: str, *, specs: Optional[dict[str, Any]] = None,
                             threshold_count: int = 12) -> tuple[list[str], bool]:
    """自适应选择：工具少 → 全给；工具多 → 只给路由结果。

    返回 ``(工具名列表, 是否做了路由)``，供 planner 注入与观测。

    E2/05：先摘掉**不可作为计划步骤**的工具（`NOT_PLANNABLE_TOOLS`），再谈路由。
    必须在这里摘、且必须在**计数之前**摘：

    * 工具数 ≤ `threshold_count` 时走"全给"分支，返回的就是**整个** `specs.keys()`
      ——不摘就会原样交给 planner（当前 9 个工具没超过阈值 12，所以真正生效的是这一路）；
    * 摘在 `route_tools` **之前**，被摘的名字才不会占 top-k 名额、也不会进 IDF 语料
      （否则它会把一个真实工具挤出候选）。

    提示词侧（`planner.md`）另有一份清单，是同一件事的**预防**；执行器侧
    （`nodes._run_one_step`）是**检测**。三处都读同一个常量，不许各写一份名单。
    """
    if specs is None:
        from .specs import TOOL_SPECS

        specs = TOOL_SPECS
    from .specs import NOT_PLANNABLE_TOOLS

    plannable = {k: v for k, v in specs.items() if k not in NOT_PLANNABLE_TOOLS}
    all_names = list(plannable.keys())
    if len(all_names) <= max(1, threshold_count):
        return all_names, False
    return route_tools(query, specs=plannable), True
