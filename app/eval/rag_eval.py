"""RAG 评估（八股文 03.9）：检索命中率 + 忠实度。

```bash
python -m app.eval.rag_eval                 # 默认 top-k=4，打印 markdown 表
python -m app.eval.rag_eval --top-k 6 --json
```

指标：
- **命中率**：`hit@k`（任一相关文档进 top-k）、`recall@k`（相关文档召回比）、`mrr`（首个命中的倒数排名）
- **忠实度**：`faithfulness = 0.6*number_grounding + 0.4*token_coverage`
  —— **确定性下界**：答案里的数字/实词能否在检索到的文档里找到。
  数字是幻觉里最危险的部分，权重更高；无数字时退化为纯 token 覆盖。

⚠ **真实忠实度需要 LLM judge**（判断"语义是否被文档支持"而非"字面是否出现"）。
这里给的是可离线复现的下界，真实判读标 `[待真实验证]`（记入 pending-real）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.text import tokenize

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
# 常见虚词，参与覆盖率会虚高
_STOP = {"的", "是", "在", "与", "和", "了", "吗", "什么", "怎么", "哪些", "为", "不", "有", "个"}


def number_grounding(answer: str, docs: list[dict]) -> Optional[float]:
    """答案中的数字有多少能在文档里找到。无数字 → None（不参与加权）。"""
    numbers = _NUMBER_RE.findall(answer or "")
    if not numbers:
        return None
    text = " ".join(str(d.get("text") or "") for d in docs)
    hit = sum(1 for n in numbers if n in text)
    return hit / len(numbers)


def token_coverage(answer: str, docs: list[dict]) -> float:
    """答案的实词（2-gram + 英文词）被文档覆盖的比例。"""
    terms = tokenize(answer, keep_single_cjk=False) - _STOP
    if not terms:
        return 1.0
    text = " ".join(str(d.get("text") or "") for d in docs)
    doc_terms = tokenize(text, keep_single_cjk=False)
    return len(terms & doc_terms) / len(terms)


def faithfulness(answer: str, docs: list[dict]) -> dict[str, Any]:
    ng = number_grounding(answer, docs)
    tc = token_coverage(answer, docs)
    score = tc if ng is None else 0.6 * ng + 0.4 * tc
    return {"number_grounding": ng, "token_coverage": round(tc, 4),
            "faithfulness": round(score, 4)}


def hit_at_k(retrieved: list[dict], relevant: tuple[str, ...], k: int) -> Optional[bool]:
    """任一相关文档进 top-k。无相关文档（负例）→ None（不参与均值）。"""
    if not relevant:
        return None
    top = [str(r.get("source") or "") for r in retrieved[:k]]
    return any(src in top for src in relevant)


def recall_at_k(retrieved: list[dict], relevant: tuple[str, ...], k: int) -> Optional[float]:
    if not relevant:
        return None
    top = {str(r.get("source") or "") for r in retrieved[:k]}
    return len(top & set(relevant)) / len(relevant)


def mrr(retrieved: list[dict], relevant: tuple[str, ...]) -> Optional[float]:
    if not relevant:
        return None
    for i, r in enumerate(retrieved, start=1):
        if str(r.get("source") or "") in relevant:
            return 1.0 / i
    return 0.0


def seed_corpus(store, corpus=None, tenant: Optional[str] = None) -> int:
    """把语料灌进知识库（幂等：先按 source 清理再写）。"""
    from .rag_golden import CORPUS

    docs = corpus if corpus is not None else CORPUS
    total = 0
    for source, text in docs:
        try:
            total += int(store.add(text, source=source, tenant=tenant) or 0)
        except TypeError:  # 兼容不带 tenant 的实现
            total += int(store.add(text, source=source) or 0)
    return total


def default_store():
    """评估专用知识库（**独立于应用知识库**）。

    用 `data/knowledge.db` 会让评估既污染生产知识、又被里面的历史数据干扰
    （实测检索结果里混进过别的测试写入的 `guard_test` 文档），指标就不可信了。
    """
    from app.core.tools.knowledge_tool import KnowledgeStore

    return KnowledgeStore(Path("data/eval_rag.db"))


def evaluate(*, top_k: int = 4, store=None, cases=None, seed: bool = True) -> dict[str, Any]:
    """跑一遍检索 + 忠实度评估。知识库为空时**明确报错**，绝不静默给 0 分通过。"""
    from .rag_golden import golden_cases

    cases = cases if cases is not None else golden_cases()
    if store is None:
        store = default_store()
    if seed:
        seed_corpus(store)

    rows: list[dict[str, Any]] = []
    for case in cases:
        try:
            retrieved = store.search(case.query, top_k=top_k) or []
        except Exception as exc:
            rows.append({"query": case.query, "error": str(exc), "retrieved": []})
            continue
        faith = faithfulness(case.answer, retrieved) if case.answer else {}
        rows.append({
            "query": case.query,
            "relevant": list(case.relevant),
            "retrieved": [r.get("source") for r in retrieved],
            "hit": hit_at_k(retrieved, case.relevant, top_k),
            "recall": recall_at_k(retrieved, case.relevant, top_k),
            "mrr": mrr(retrieved, case.relevant),
            **faith,
        })

    def _mean(key: str) -> Optional[float]:
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else None

    hits = [r["hit"] for r in rows if r.get("hit") is not None]
    return {
        "top_k": top_k,
        "cases": len(rows),
        "hit_rate": round(sum(1 for h in hits if h) / len(hits), 4) if hits else None,
        "avg_recall": _mean("recall"),
        "mrr": _mean("mrr"),
        "avg_faithfulness": _mean("faithfulness"),
        "avg_token_coverage": _mean("token_coverage"),
        "details": rows,
        "note": "忠实度为**确定性下界**（数字/实词覆盖），真实语义判读需 LLM judge → [待真实验证]",
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# RAG 评估（检索命中率 + 忠实度）",
        "",
        f"- top_k：{report['top_k']} · 用例：{report['cases']}",
        f"- **hit@{report['top_k']}**：{report['hit_rate']}",
        f"- **avg recall@{report['top_k']}**：{report['avg_recall']}",
        f"- **MRR**：{report['mrr']}",
        f"- **avg faithfulness**（下界）：{report['avg_faithfulness']}",
        "",
        "| query | 相关文档 | 检索到 | hit | recall | faithfulness |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["details"]:
        lines.append(
            f"| {r['query']} | {'、'.join(r.get('relevant') or []) or '（负例）'} "
            f"| {'、'.join([x for x in (r.get('retrieved') or []) if x][:3])} "
            f"| {r.get('hit')} | {r.get('recall')} | {r.get('faithfulness')} |")
    lines += ["", f"> {report['note']}"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 检索/忠实度评估")
    ap.add_argument("--top-k", type=int, default=4)
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--no-seed", action="store_true", help="不灌语料（用既有知识库）")
    args = ap.parse_args()

    report = evaluate(top_k=args.top_k, seed=not args.no_seed)
    if report["hit_rate"] is None:
        print("知识库为空或无可评估用例——评估未执行（不是「零分通过」）。", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
          else render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
