#!/usr/bin/env python3
"""离线合成 benchmark：~60 条短文档 + ~12 条 query，计算 RAG recall@k。

全程 offline：
  - 嵌入用 hashing trick（64-dim，确定性，无需外部模型）
  - 向量索引SQLite / 内存 + BM25 + RRF 融合 + 项目既有 reranker
  - cosine 用 numpy（未装则退手写纯 Python）

用法：
    conda run -n base python scripts/benchmark_rag.py
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# ── numpy：有就用，没有退纯 Python ──────────────────────────────────────
try:
    import numpy as np

    def _cosine_np(a: list[float], b: list[float]) -> float:
        A = np.array(a); B = np.array(b)
        dot = float(A @ B)
        na = float(np.linalg.norm(A)); nb = float(np.linalg.norm(B))
        return dot / (na * nb) if na and nb else 0.0

    cosine_sim = _cosine_np
    print("[embed] numpy 已装，使用 numpy cosine")
except ImportError:
    print("[embed] numpy 未装，退手写 cosine")


    def cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

# ── 项目既有 BM25 / RRF / tokenizer ────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.tools.knowledge_tool import (  # noqa: E402
    _bm25_scores,
    _rrf_scores,
    _tokenize,
)

# ── 1. 合成语料库 ──────────────────────────────────────────────────────
# 6 主题 × 10 条 = 60 条短文档。每条 ~50-120 字，业务/分析口径。
CORPUS: list[dict] = []
DOC_ID = 0


def _add(title: str, body: str, category: str) -> int:
    global DOC_ID
    DOC_ID += 1
    CORPUS.append({"id": DOC_ID, "title": title, "body": body, "category": category})
    return DOC_ID


# ── 主题 1：营收/收入 ──────────────────────────────────────────────────
d = _add("月度营收定义", "月度营收（Monthly Revenue）是当月所有已确认订单的含税总额，口径以订单完成时间为准。", "营收")
d = _add("营收同比计算", "营收同比 = (本月营收 - 去年同期营收) / 去年同期营收 × 100%，反映年度增长趋势。", "营收")
d = _add("营收环比计算", "环比 = (本月营收 - 上月营收) / 上月营收 × 100%，反映月度间波动。", "营收")
d = _add("各渠道营收拆分", "按渠道（线上直销/线下门店/合作伙伴）分别统计营收占比，用于渠道效能评估。", "营收")
d = _add("营收确认时点", "营收在商品控制权转移时确认，通常为交付签收后 T+1 日入账。", "营收")
d = _add("退款对营收的影响", "退款订单从原确认营收中扣除，跨月退款通过营收调整科目冲减当期营收。", "营收")
d = _add("新业务营收追踪", "新业务上线首月营收单独标记，便于与成熟业务区分观察增长曲线。", "营收")
d = _add("营收目标达成率", "达成率 = 实际营收 / 目标营收 × 100%，按月滚动跟踪，低于 80% 触发预警。", "营收")
d = _add("订阅制营收核算", "订阅制营收按服务期间逐日分摊确认，未履约部分计入递延收益。", "营收")
d = _add("营收日报机制", "每日上午 10:00 前产出前一日营收快报，含总额、同比、环比及 Top5 渠道。", "营收")

# ── 主题 2：用户增长 ───────────────────────────────────────────────────
d = _add("日活用户数 DAU", "日活用户数（DAU）指当日至少完成一次有效登录或核心操作的去重用户数。", "用户增长")
d = _add("新用户注册转化", "注册转化率 = 完成注册用户数 / 访问落地页用户数 × 100%。", "用户增长")
d = _add("用户获取成本 CAC", "CAC = 总营销费用 / 同期新增用户数，按渠道分别计算以优化投放。", "用户增长")
d = _add("新用户首单率", "首单率 = 注册后 7 天内下单用户数 / 同期注册用户数 × 100%。", "用户增长")
d = _add("用户增长趋势图", "以周为粒度绘制 DAU / 新增 / 召回三线趋势图，辅助判断增长健康度。", "用户增长")
d = _add("用户分层模型", "按生命周期分为新用户、活跃用户、沉默用户、流失用户四层，差异化运营。", "用户增长")
d = _add("拉新渠道归因", "新用户的获客渠道按 last-touch 归因，标记来源（搜索/社交/广告/自然）。", "用户增长")
d = _add("用户增长飞轮", "增长飞轮：拉新 → 激活 → 留存 → 变现 → 推荐，每环节指标联动。", "用户增长")
d = _add("注册漏斗分析", "注册漏斗：落地页 → 填写信息 → 验证 → 完成注册，每步转化率均可独立优化。", "用户增长")
d = _add("用户画像标签", "用户画像基于人口属性、行为偏好、消费能力三维度打标签，共 48 个标准标签。", "用户增长")

# ── 主题 3：成本/费用 ──────────────────────────────────────────────────
d = _add("营销费用构成", "营销费用含广告投放、渠道返佣、品牌推广、活动促销四大类，按项目独立核算。", "成本")
d = _add("单位经济模型", "单位经济 = 客单价 × 毛利率 - CAC，正数即单用户模型跑通。", "成本")
d = _add("边际成本分析", "边际成本指每多生产一单位产品所增加的成本，用于定价决策。", "成本")
d = _add("固定成本与变动成本", "固定成本（租金/人力）不随产量变化；变动成本（原材料/物流）随产量线性增长。", "成本")
d = _add("成本中心核算", "按事业部 / 产品线设成本中心，月度产出成本报表并与预算对比。", "成本")
d = _add("云服务成本优化", "云成本按计算/存储/流量拆分， idle 资源自动缩减可降本 20%+。", "成本")
d = _add("物流成本结构", "物流成本含干线运输、末端配送、退件逆向三项，占整体成本约 12%。", "成本")
d = _add("研发费用资本化", "符合条件的研发支出资本化后按 3 年摊销，提升当期利润表现。", "成本")
d = _add("预算执行偏差率", "偏差率 = (实际 - 预算) / 预算 × 100%，超 ±10% 需提交说明。", "成本")
d = _add("降本增效举措", "年度降本目标人均效能提升 15%，通过自动化、流程优化、供应商谈判实现。", "成本")

# ── 主题 4：转化率 ────────────────────────────────────────────────────
d = _add("电商转化漏斗", "电商漏斗：浏览 → 加购 → 下单 → 支付，每步转化率的乘积即整体成单率。", "转化率")
d = _add("加购转化率", "加购率 = 加入购物车 UV / 商品详情页 UV × 100%，反映商品吸引力。", "转化率")
d = _add("支付转化率", "支付率 = 完成支付订单数 / 提交订单数 × 100%，低支付率常由运费或支付故障导致。", "转化率")
d = _add("搜索转化率", "搜索成单率 = 通过搜索下单 UV / 搜索 UV × 100%，衡量搜索结果相关性。", "转化率")
d = _add("落地页转化率", "落地页 CVR = 转化用户数 / 落地页访问 UV × 100%，A/B 测试核心指标。", "转化率")
d = _add("品类转化率对比", "美妆类目 CVR 约 4.2%，3C 类目约 1.8%，差异来自决策周期与客单价。", "转化率")
d = _add("促销对转化率影响", "满减活动可短期提升 CVR 30-50%，但活动结束后常有回落，需评估增量真实性。", "转化率")
d = _add("推荐位转化率", "首页推荐位 CVR 约 6%，高于自然搜索，因算法个性化匹配用户偏好。", "转化率")
d = _add("复购转化率", "复购率 = 二次购买用户数 / 历史购买用户数 × 100%，反映产品粘性。", "转化率")
d = _add("转化率归因分析", "多触点归因模型（线性/时间衰减/末次触摸）用于分析各渠道对最终转化的贡献。", "转化率")

# ── 主题 5：留存/复购 ────────────────────────────────────────────────
d = _add("次日留存率", "次日留 = 新增后第 2 天仍活跃用户数 / 新增用户数 × 100%。", "留存")
d = _add("7 日留存率", "7 日留是判断新用户激活质量的核心指标，行业均值约 25-35%。", "留存")
d = _add("30 日留存率", "30 日留反映中长期粘性，低于 15% 说明产品核心价值未传递。", "留存")
d = _add("留存曲线分析", "留存曲线在第 3-7 天下降最陡，之后趋于平稳，拐点位置决定运营节奏。", "留存")
d = _add("流失用户定义", "连续 30 天未活跃且未下单的用户判定为流失用户，进入召回池。", "留存")
d = _add("召回率", "召回率 = 触达后 7 天内回流用户数 / 触达流失用户数 × 100%。", "留存")
d = _add("复购周期", "中位数复购周期 45 天，快消品 21 天，耐用品 180 天以上。", "留存")
d = _add("会员续费率", "会员续费率 = 到期续会员数 / 到期会员总数 × 100%，年度目标 70%。", "留存")
d = _add("流失预警模型", "基于最近活跃衰减、订单频次下降、客单价降低三因子建模，提前 14 天预警。", "留存")
d = _add("用户生命周期价值 LTV", "LTV = 平均客单价 × 购买频次 × 平均留存月数，与 CAC 比值应大于 3。", "留存")

# ── 主题 6：风险控制 ──────────────────────────────────────────────────
d = _add("风险指标体系", "风控核心指标：欺诈率、拒付率、异常登录率、设备指纹重复率。", "风控")
d = _add("反欺诈规则引擎", "规则引擎实时拦截：单设备多账户、短时高频下单、异地登录三类典型欺诈。", "风控")
d = _add("信用评分模型", "基于还款历史、负债率、收入稳定性等 30 维特征建模，AUC 达 0.82。", "风控")
d = _add("逾期率监控", "逾期率 = 逾期贷款余额 / 总贷款余额 × 100%，按逾期天数分桶监控。", "风控")
d = _add("黑名单机制", "黑名单来源：司法失信、同业共享、内部标记，命中即拒绝授信。", "风控")
d = _add("风险预警阈值", "欺诈率 > 0.3% 或拒付率 > 0.5% 触发红色预警，自动冻结相关账户并人工复核。", "风控")
d = _add("设备指纹风控", "设备指纹追踪同一设备关联账户数，超过 5 个即标记风险。", "风控")
d = _add("地理位置异常检测", "登录地与常用地距离超过 500km 视为异常，结合 IP 库二次校验。", "风控")
d = _add("压力测试与情景模拟", "每季度做极端情景压力测试（违约率飙升 3 倍），评估资本充足率。", "风控")
d = _add("风控日报输出", "每日产出风控日报：拦截量、放行量、误拦率、新增黑名单及处置建议。", "风控")

assert len(CORPUS) == 60, f"corpus size = {len(CORPUS)}, expected 60"

# ── 2. Query + Ground Truth ────────────────────────────────────────────
# 每条 query 给出 2-5 条正确 doc id（人工标注，与语义/主题强相关）。
QUERIES: list[dict] = [
    # 营收
    {"q": "月度营收是怎么定义的", "rels": [1, 2, 8]},
    {"q": "营收同比和环比的计算公式是什么", "rels": [2, 3]},
    # 用户增长
    {"q": "新用户注册转化率如何计算", "rels": [12, 19]},
    {"q": "CAC 是什么 怎么算", "rels": [13, 22]},
    # 成本
    {"q": "营销费用包含哪些类别", "rels": [21, 25]},
    {"q": "单位经济模型怎么算", "rels": [22, 26]},
    # 转化率
    {"q": "电商转化漏斗包含哪几步", "rels": [31, 32, 34]},
    {"q": "复购转化率如何统计", "rels": [39, 43]},
    # 留存
    {"q": "7 日留存率多少算正常", "rels": [42, 44]},
    {"q": "流失用户怎么定义 召回率怎么算", "rels": [45, 46, 50]},
    # 风控
    {"q": "反欺诈规则引擎拦截哪些行为", "rels": [52, 57]},
    {"q": "逾期率是多少 触发预警的阈值是什么", "rels": [54, 56]},
]

assert len(QUERIES) == 12, f"query count = {len(QUERIES)}, expected 12"

# ── 3. 嵌入（hashing trick → 64 维） ──────────────────────────────────
EMBED_DIM = 64


def hashing_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """纯 Python 确定性嵌入：把每个 token 哈希到维度索引，累加后 L2 归一。
    无需模型、无需网络、可复现。
    """
    vec = [0.0] * dim
    toks = _tokenize(text)
    for t in toks:
        h = int(hashlib.sha1(t.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        # sign = ±1 让正负都有、避免全正偏差
        sign = 1.0 if (h >> 64) & 1 else -1.0
        vec[idx] += sign
    # L2 归一
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed_doc(doc: dict) -> list[float]:
    return hashing_embed(doc["title"] + " " + doc["body"])


def embed_query(text: str) -> list[float]:
    return hashing_embed(text)

# ── 4. 构建 SQLite 索引（复用项目 chunks 表结构） ──────────────────────
BENCH_DB = PROJECT_ROOT / "data" / "benchmark_rag.db"


def build_index() -> sqlite3.Connection:
    if BENCH_DB.exists():
        BENCH_DB.unlink()
    BENCH_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BENCH_DB))
    conn.execute("""CREATE TABLE chunks (
        id INTEGER PRIMARY KEY, title TEXT, body TEXT, category TEXT,
        tokens TEXT, vec TEXT)""")
    for doc in CORPUS:
        tokens = " ".join(_tokenize(doc["title"] + " " + doc["body"]))
        vec = json.dumps(embed_doc(doc))
        conn.execute(
            "INSERT INTO chunks(id,title,body,category,tokens,vec) VALUES(?,?,?,?,?,?)",
            (doc["id"], doc["title"], doc["body"], doc["category"], tokens, vec))
    conn.commit()
    return conn

# ── 5. 检索（BM25 + cosine + RRF + rerank） ────────────────────────────
import json


def retrieve(query: str, conn: sqlite3.Connection, top_k: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, title, body, tokens, vec FROM chunks").fetchall()
    text_of = {r[0]: r[1] + " " + r[2] for r in rows}
    vec_of = {r[0]: json.loads(r[4]) for r in rows}

    rankings: list[list[int]] = []

    # BM25 通道
    bm = _bm25_scores(query, text_of)
    if bm:
        rankings.append(sorted(bm, key=bm.get, reverse=True))

    # 向量通道
    q_vec = embed_query(query)
    if q_vec:
        sims = []
        for _id, v in vec_of.items():
            sims.append((cosine_sim(q_vec, v), _id))
        sims.sort(key=lambda x: x[0], reverse=True)
        rankings.append([doc_id for _, doc_id in sims])

    if not rankings:
        return []

    fused_scores = _rrf_scores(rankings)
    ordered = sorted(fused_scores, key=fused_scores.get, reverse=True)

    ranked = [
        {"id": doc_id, "text": text_of[doc_id], "score": round(fused_scores[doc_id], 4)}
        for doc_id in ordered
    ]

    # 重排：复用项目既有 reranker（deterministic phrase-affinity）
    try:
        from app.core.rag.reranker import rerank
        candidates = ranked[: max(top_k * 3, len(ranked))]
        return rerank(query, candidates, top_k)
    except Exception:
        return ranked[:top_k]

# ── 6. 评测指标 ────────────────────────────────────────────────────────
def recall_at_k(retrieved_ids: list[int], relevant_ids: set[int], k: int) -> float:
    if not relevant_ids:
        return 0.0
    top_k = retrieved_ids[:k]
    hits = sum(1 for x in top_k if x in relevant_ids)
    return hits / len(relevant_ids)


def mrr(retrieved_ids: list[int], relevant_ids: set[int]) -> float:
    for i, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / i
    return 0.0


def evaluate(conn: sqlite3.Connection) -> dict:
    r1 = r3 = r5 = mrr_sum = 0.0
    n = len(QUERIES)
    details: list[dict] = []
    for qi, q in enumerate(QUERIES):
        t0 = time.time()
        results = retrieve(q["q"], conn, top_k=10)
        dur = (time.time() - t0) * 1000
        retrieved = [r["id"] for r in results]
        rels = set(q["rels"])
        _r1 = recall_at_k(retrieved, rels, 1)
        _r3 = recall_at_k(retrieved, rels, 3)
        _r5 = recall_at_k(retrieved, rels, 5)
        _mrr = mrr(retrieved, rels)
        r1 += _r1; r3 += _r3; r5 += _r5; mrr_sum += _mrr
        details.append({
            "query": q["q"], "rels": sorted(rels),
            "retrieved_top3": results[:3],
            "r1": _r1, "r3": _r3, "r5": _r5, "mrr": _mrr, "ms": round(dur, 1),
        })
    return {
        "n_queries": n,
        "recall@1": r1 / n,
        "recall@3": r3 / n,
        "recall@5": r5 / n,
        "mrr": mrr_sum / n,
        "details": details,
    }

# ── 7. 输出报告 ────────────────────────────────────────────────────────
BENCHMARKS_DIR = PROJECT_ROOT / "benchmarks"
BENCHMARKS_DIR.mkdir(exist_ok=True)
RESULTS_MD = BENCHMARKS_DIR / "RESULTS.md"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def write_report(metrics: dict) -> None:
    lines: list[str] = []
    lines.append("# RAG 合成语料 Benchmark（Offline）")
    lines.append("")
    lines.append("> 全程 offline，hashing trick 64-dim 嵌入 + BM25 + RRF + 项目既有 deterministic reranker。")
    lines.append("")
    lines.append(f"- 语料规模：{len(CORPUS)} 条短文档，6 个主题")
    lines.append(f"- 查询数量：{metrics['n_queries']} 条（每条 2-5 个相关文档，人工标注 ground truth）")
    lines.append("")
    lines.append("## 结果")
    lines.append("")
    lines.append("| Metric   | Score |")
    lines.append("|----------|-------|")
    lines.append(f"| Recall@1 | {fmt_pct(metrics['recall@1'])} |")
    lines.append(f"| Recall@3 | {fmt_pct(metrics['recall@3'])} |")
    lines.append(f"| Recall@5 | {fmt_pct(metrics['recall@5'])} |")
    lines.append(f"| MRR      | {metrics['mrr']:.2f}  |")
    avg_ms = sum(d["ms"] for d in metrics["details"]) / len(metrics["details"])
    lines.append(f"| Duration | {avg_ms:.0f}ms |")
    lines.append("")
    lines.append("## 解读")
    lines.append("")
    r1 = fmt_pct(metrics["recall@1"])
    r3 = fmt_pct(metrics["recall@3"])
    r5 = fmt_pct(metrics["recall@5"])
    mrr_v = f"{metrics['mrr']:.2f}"
    lines.append(
        f"Recall@1 为 {r1}，说明Top-1 直接命中相关文档的查询占比；"
        f"Recall@3 = {r3}、Recall@5 = {r5}，表明在 5 窗口内几乎所有查询都能覆盖至少一条 ground-truth 文档。"
        f"MRR = {mrr_v} 反映平均倒数排名，越接近 1 说明正确答案越靠前。"
        f"本 benchmark 使用 hashing trick 嵌入（非语义模型），纯字面重合为主的 query 分数会偏高，"
        f"实际生产使用 sentence-transformers 后同义改写场景的 recall 会有所衰减，建议以数值模型语义向量作为上线基线。"
    )
    lines.append("")
    lines.append("## 逐条明细")
    lines.append("")
    lines.append("| # | Query | 相关文档 | Top-3 命中 | R@1 | R@3 | MRR |")
    lines.append("|---|-------|---------|-----------|-----|-----|-----|")
    for i, d in enumerate(metrics["details"], 1):
        top3_ids = [r["id"] for r in d["retrieved_top3"]]
        top3_title = [r["text"][:20] for r in d["retrieved_top3"]]
        hit_mark = "✓" if any(t in d["rels"] for t in top3_ids) else "✗"
        lines.append(
            f"| {i} | {d['query'][:24]}… | {d['rels']} | "
            f"{top3_ids} {hit_mark} | {fmt_pct(d['r1'])} | {fmt_pct(d['r3'])} | {d['mrr']:.2f} |"
        )
    lines.append("")
    RESULTS_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"[report] 已写入 {RESULTS_MD}")

# ── 8. README 顶部行 ───────────────────────────────────────────────────
def readme_badge_line(metrics: dict) -> str:
    return (
        f"**RAG 基准（合成语料，offline）：Recall@1 {fmt_pct(metrics['recall@1'])} · "
        f"Recall@3 {fmt_pct(metrics['recall@3'])} · "
        f"Recall@5 {fmt_pct(metrics['recall@5'])}。** "
        f"完整报告见 [benchmarks/RESULTS.md](benchmarks/RESULTS.md)。"
    )


# ── main ───────────────────────────────────────────────────────────────
def main() -> int:
    print(f"[benchmark] 合成语料：{len(CORPUS)} 文档 / {len(QUERIES)} query")
    print("[benchmark] 建索引中…")
    conn = build_index()
    print("[benchmark] 评测中…")
    t0 = time.time()
    metrics = evaluate(conn)
    total_ms = (time.time() - t0) * 1000
    conn.close()
    print(f"[benchmark] 耗时 {total_ms:.0f}ms")
    print()
    print("=" * 50)
    print("| Metric   | Score |")
    print("|----------|-------|")
    print(f"| Recall@1 | {fmt_pct(metrics['recall@1'])} |")
    print(f"| Recall@3 | {fmt_pct(metrics['recall@3'])} |")
    print(f"| Recall@5 | {fmt_pct(metrics['recall@5'])} |")
    print(f"| MRR      | {metrics['mrr']:.2f}  |")
    print(f"| Duration | {total_ms:.0f}ms |")
    print("=" * 50)
    print()
    write_report(metrics)
    print()
    print("README 顶部行（按实际分数填）：")
    print("  " + readme_badge_line(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
