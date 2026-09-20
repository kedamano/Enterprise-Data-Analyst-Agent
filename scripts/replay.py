"""Agent Debug Replay CLI。

重放任意 session 的 trace JSONL，替换 tool 实现（mock SQL / mock python），
快速复现并定位 agent pipeline 问题。

用法：
    # 查看 trace 摘要
    conda run -n base python scripts/replay.py show <session_id>

    # 重放：注入 mock tools，跳过 planner/executor，直接跑 analyst+reporter
    conda run -n base python scripts/replay.py run <session_id> [--tools sql_query=mock,python_analysis=mock]

    # 对比原 trace 与最后一次 replay trace 的阶段耗时
    conda run -n base python scripts/replay.py diff <session_id>
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import pathlib
import sys
import time
from typing import Any, Callable, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("replay")

# ---- 项目根 ----
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRACES_DIR = ROOT / "data" / "traces"


# ---- mock tools ----
def _mock_sql_query(params: dict) -> dict:
    return {
        "rows": [{"gvm": 100, "month": "2025-01"}, {"gvm": 120, "month": "2025-02"}],
        "columns": ["gvm", "month"],
        "mock": True,
    }


def _mock_python_analysis(params: dict) -> dict:
    return {"stdout": "mock_result = 42\nanalysis_done = True", "mock": True}


def _mock_schema_search(params: dict) -> dict:
    return {"tables": [], "columns": {}, "mock": True}


def _mock_knowledge_search(params: dict) -> dict:
    return {"results": [], "mock": True}


MOCK_TOOLS: dict[str, Callable[[dict], Any]] = {
    "sql_query": _mock_sql_query,
    "python_analysis": _mock_python_analysis,
    "schema_search": _mock_schema_search,
    "knowledge_search": _mock_knowledge_search,
}


def _parse_overrides(overrides_str: str) -> dict[str, Callable[[dict], Any]]:
    """解析 'sql_query=mock,python_analysis=mock' → {name: mock_fn}。"""
    result = {}
    for pair in overrides_str.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _, kind = pair.partition("=")
        name = name.strip()
        kind = kind.strip() or "mock"
        if kind == "mock" and name in MOCK_TOOLS:
            result[name] = MOCK_TOOLS[name]
        elif kind == "pass":
            # pass-through：保留真实工具
            continue
        else:
            logger.warning("Unknown tool override: %s=%s (skip)", name, kind)
    return result


# ---- trace 读取 ----
def _read_trace(session_id: str) -> Optional[dict]:
    trace_file = TRACES_DIR / f"{session_id}.jsonl"
    if not trace_file.exists():
        return None
    spans = []
    with trace_file.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    spans.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if not spans:
        return None
    summary = spans[-1] if spans[-1].get("type") == "summary" else {}
    return {
        "session_id": session_id,
        "trace_file": str(trace_file),
        "spans": spans,
        "summary": summary,
        "user_query": summary.get("user_query", "(unknown)"),
        "status": summary.get("status", "(unknown)"),
        "stage_count": len([s for s in spans if s.get("type") == "span"]),
        "total_duration_ms": summary.get("total_duration_ms", 0),
    }


def _list_traces() -> list[str]:
    if not TRACES_DIR.exists():
        return []
    return sorted(
        p.stem for p in TRACES_DIR.glob("*.jsonl")
    )


# ---- 命令实现 ----
def cmd_show(session_id: str) -> None:
    trace = _read_trace(session_id)
    if trace is None:
        logger.error("Trace not found: %s", session_id)
        logger.info("Available sessions: %s", _list_traces()[:10])
        return
    print(json.dumps(trace, indent=2, ensure_ascii=False))


def cmd_run(session_id: str, tool_overrides_str: str = "sql_query=mock,python_analysis=mock") -> None:
    """重放 trace（mock tools + MockLLM）。

    实际注入方式：通过 app.core.agents.data_analyst.graph._apply_tool_overrides
    临时替换 REGISTRY 中的 tool 实现，再调 run_analysis(replay_mode=True)。
    """
    trace = _read_trace(session_id)
    if trace is None:
        logger.error("Trace not found: %s", session_id)
        return

    overrides = _parse_overrides(tool_overrides_str)
    user_query = trace["user_query"]
    ts = int(time.time())
    replay_id = f"{session_id}-replay-{ts}"

    logger.info("Replaying session %s → %s", session_id, replay_id)
    logger.info("User query: %s", user_query)
    logger.info("Tool overrides: %s", list(overrides.keys()))

    try:
        from app.config import get_settings
        from app.core.agents.data_analyst.graph import (
            _apply_tool_overrides,
            _clear_tool_overrides,
            run_analysis,
        )
    except ImportError as e:
        logger.error("Import failed: %s", e)
        return

    _apply_tool_overrides(overrides)
    try:
        # replay_mode 跳过 context/planner/executor，从真实 plan 注入 state.plan
        state = run_analysis(
            session_id=replay_id,
            user_query=user_query,
            replay_mode=True,
        )
    except Exception as e:
        logger.error("Replay failed: %s", e)
        raise
    finally:
        _clear_tool_overrides()

    print(json.dumps({
        "replay_session_id": replay_id,
        "status": state.status,
        "report_snippet": (state.report or "")[:500],
        "trace_file": str(TRACES_DIR / f"{replay_id}.jsonl"),
        "metadata_keys": list(state.metadata.keys()),
    }, indent=2, ensure_ascii=False))


def cmd_diff(session_id: str) -> None:
    """对比原始 trace 与最后一次 replay trace。"""
    original = _read_trace(session_id)
    if original is None:
        logger.error("Original trace not found: %s", session_id)
        return

    # 找最后一次 replay
    replay_traces = sorted(TRACES_DIR.glob(f"{session_id}-replay-*.jsonl"))
    if not replay_traces:
        logger.error("No replay traces found for %s", session_id)
        return

    replay_trace_file = replay_traces[-1]
    replay = _read_trace(replay_trace_file.stem)
    if replay is None:
        logger.error("Failed to parse replay: %s", replay_trace_file)
        return

    print("=" * 70)
    print(f"  Diff: {session_id} (original) vs {replay_trace_file.stem} (replay)")
    print("=" * 70)
    print(f"  Duration: {original['total_duration_ms']}ms → {replay.get('total_duration_ms', 0)}ms")
    print(f"  Status:   {original['status']} → {replay.get('status', '?')}")
    print(f"  Stages:   {original['stage_count']} → {replay.get('stage_count', '?')}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Debug Replay CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", help="查看 trace 摘要")
    p_show.add_argument("session_id", help="原始 trace 的 session_id")

    p_run = sub.add_parser("run", help="重放 trace (mock tools)")
    p_run.add_argument("session_id", help="原始 trace 的 session_id")
    p_run.add_argument(
        "--tools", default="sql_query=mock,python_analysis=mock",
        help="tool 覆盖列表（逗号分隔，tool_name=mock|pass）",
    )

    p_diff = sub.add_parser("diff", help="对比原 trace 与最后一次 replay trace")
    p_diff.add_argument("session_id", help="原始 trace 的 session_id")

    args = parser.parse_args()
    if args.command == "show":
        cmd_show(args.session_id)
    elif args.command == "run":
        cmd_run(args.session_id, args.tools)
    elif args.command == "diff":
        cmd_diff(args.session_id)


if __name__ == "__main__":
    main()
