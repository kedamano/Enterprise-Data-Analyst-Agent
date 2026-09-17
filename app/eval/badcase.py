"""D39：badcase 回流 —— 把"失败"变成**可复跑的资产**。

为什么需要它
------------
D38 的真实基线 7 条里 6 条断言失败，但**跑完就散了**：没落盘、没法复跑、没法变成回归。
下一轮改动究竟是"修好了"还是"又弄坏了"，只能靠人肉比对两份 markdown——这正是
`docs/progress/` 里堆了一排 `eval-*-INVALID/CONTAMINATED/INCOMPLETE` 的原因。

本模块提供四件事
----------------
1. `record_from_report()` —— 从 eval 报告挑出**该记的**失败用例落盘；
2. 同一 case 反复失败**只留一条**，`runs` 累加（否则每跑一次就多一堆文件）；
3. `replay()` —— 拿落盘的 badcase 原样重跑，直接回答"修好没有"；
4. `promote_draft()` —— 生成**待人工审阅**的 golden 片段，**不自动写入**
   （断言该立什么需要人判断；让模型给自己出题等于把门槛交给被考的人）。

**不记什么**（重要）
--------------------
- `SKIPPED`：`requires_real` 在 mock 下没跑——**没跑不等于失败**（铁律 6）；
- `DEGRADED`：限流/余额导致的降级**不是模型的错**，记进来会污染复跑结论。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DEFAULT_DIR = Path("data/badcases")
# 已有成功记录、说明本轮通过——不记为 badcase 的状态
_NOT_BAD = ("SKIPPED", "DEGRADED")


@dataclass
class BadCase:
    case_id: str
    query: str = ""
    reasons: list[str] = field(default_factory=list)
    status: str = ""
    findings: int = 0
    executed_tools: list[str] = field(default_factory=list)
    report_excerpt: str = ""
    first_seen: str = ""
    last_seen: str = ""
    runs: int = 0
    degraded: bool = False


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _query_of(case_id: str) -> str:
    """从 golden 反查原始问题——复跑必须用**同一句话**，否则不可比。"""
    try:
        from .golden import GOLDEN

        return next((c.query for c in GOLDEN if c.id == case_id), "")
    except Exception:
        return ""


def record_from_report(report: dict[str, Any],
                       out_dir: Path | str = DEFAULT_DIR) -> list[Path]:
    """把报告里失败的用例落盘（同一 case 覆盖更新、`runs` 累加）。返回写入的文件。"""
    out = Path(out_dir)
    written: list[Path] = []
    for detail in report.get("cases_detail") or []:
        if not _is_badcase(detail):
            continue
        out.mkdir(parents=True, exist_ok=True)
        case_id = str(detail.get("case_id") or "unknown")
        path = out / f"{case_id}.json"
        prev: dict[str, Any] = {}
        if path.exists():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                prev = {}
        now = _now()
        bc = BadCase(
            case_id=case_id,
            query=prev.get("query") or _query_of(case_id),
            reasons=list(detail.get("failed_assertions") or []),
            status=str(detail.get("status") or ""),
            findings=int(detail.get("findings") or 0),
            executed_tools=list(detail.get("executed_tools") or []),
            report_excerpt=str(detail.get("report_text") or "")[:1200],
            first_seen=prev.get("first_seen") or now,
            last_seen=now,
            runs=int(prev.get("runs") or 0) + 1,
            degraded=bool(detail.get("degraded")),
        )
        path.write_text(json.dumps(asdict(bc), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        written.append(path)
    return written


def _is_badcase(detail: dict[str, Any]) -> bool:
    if str(detail.get("status") or "") in _NOT_BAD:
        return False
    if detail.get("degraded"):
        return False
    if str(detail.get("status")) != "FINISH":
        return True
    return not bool(detail.get("assertions_ok"))


def load_all(out_dir: Path | str = DEFAULT_DIR) -> list[BadCase]:
    out = Path(out_dir)
    cases: list[BadCase] = []
    for path in sorted(out.glob("*.json")) if out.exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        known = {k: v for k, v in data.items() if k in BadCase.__dataclass_fields__}
        cases.append(BadCase(**known))
    return cases


def replay(out_dir: Path | str = DEFAULT_DIR, *, only: str | None = None,
           mode: str = "real",
           runner: Callable[..., dict[str, Any]] | None = None) -> dict[str, list[str]]:
    """复跑落盘的 badcase → `{"fixed": [...], "still_bad": [...]}`。

    `runner` 可注入（默认用 `app.eval.runner.evaluate`），便于离线测试。
    """
    cases = load_all(out_dir)
    ids = [c.case_id for c in cases if only is None or c.case_id == only]
    if not ids:
        return {"fixed": [], "still_bad": []}
    if runner is None:
        from .runner import evaluate as runner  # type: ignore[assignment]
    report = runner(mode, only_real=False, ids=ids)
    by_id = {d.get("case_id"): d for d in (report.get("cases_detail") or [])}
    fixed: list[str] = []
    still: list[str] = []
    for cid in ids:
        detail = by_id.get(cid)
        if detail and not _is_badcase(detail):
            fixed.append(cid)
        else:
            still.append(cid)
    return {"fixed": fixed, "still_bad": still}


def promote_draft(case: BadCase) -> str:
    """生成**待人工审阅**的 golden 片段。**不写文件**——写不写、断言什么，由人定。"""
    reasons = "\n".join(f"#   - {r}" for r in (case.reasons or ["（无失败原因记录）"]))
    return (
        "# ——— badcase 固化草稿（请人工审阅后再粘贴进 ANALYST_GOLDEN）———\n"
        "# TODO: 选择断言。失败原因里既有「措辞没命中」（脆弱、应改断言）\n"
        "#       也有「确实没做到」（该保留并作为回归）。别照抄 must_find。\n"
        f"# 真实失败 {case.runs} 次（首见 {case.first_seen}，最近 {case.last_seen}）\n"
        f"{reasons}\n"
        "GoldenCase(\n"
        f'    id="{case.case_id}",\n'
        f'    query="{case.query}",\n'
        "    # TODO: 断言（建议优先结构化字段：min_findings / min_numeric_claims /\n"
        "    #       expect_quality_codes / must_not_have_quality_codes）\n"
        "    requires_real=True,\n"
        "),\n"
    )


def main() -> None:  # pragma: no cover - 薄 CLI
    import argparse

    ap = argparse.ArgumentParser(description="badcase 回流")
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--list", action="store_true", help="列出已落盘的 badcase")
    ap.add_argument("--replay", action="store_true", help="复跑并报告修好没有")
    ap.add_argument("--only", default=None, help="只处理指定 case id")
    ap.add_argument("--promote", default=None, help="生成某条 badcase 的 golden 草稿")
    ap.add_argument("--mode", choices=["mock", "real"], default="real")
    args = ap.parse_args()

    if args.list or not (args.replay or args.promote):
        for c in load_all(args.dir):
            print(f"[{c.status}] {c.case_id}  失败 {c.runs} 次  最近 {c.last_seen}")
            for r in c.reasons[:3]:
                print(f"    - {r}")
        return
    if args.promote:
        hit = [c for c in load_all(args.dir) if c.case_id == args.promote]
        print(promote_draft(hit[0]) if hit else f"没有落盘的 badcase: {args.promote}")
        return
    res = replay(args.dir, only=args.only, mode=args.mode)
    print(f"已修复: {res['fixed'] or '（无）'}")
    print(f"仍未过: {res['still_bad'] or '（无）'}")


if __name__ == "__main__":  # pragma: no cover
    main()
