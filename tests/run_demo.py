"""End-to-end demo (offline, Mock LLM).

Run with the project venv:
    python tests/run_demo.py

It exercises the full pipeline against the bundled SQLite sample and prints the
generated business report plus the tool-execution trace.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许 `python tests/run_demo.py` 从任意工作目录直接运行：
# 1) 把项目根加入 sys.path；2) 切到项目根（.env / data/ 均为相对路径）。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)
# 本脚本的契约是「offline, Mock LLM」演示：默认强制 Mock，防止 .env 里有真实
# key 时意外烧钱（显式设 MOCK_LLM=false 可覆盖，走真实 LLM）。
os.environ.setdefault("MOCK_LLM", "true")

from app.core.agents.data_analyst.graph import run_analysis


def main() -> None:
    query = "分析最近营收变化的原因，按地区和产品维度下钻，并给出建议"
    print(f">>> 用户请求: {query}\n")
    state = run_analysis(session_id="demo", user_query=query)

    print(f"状态: {state.status} | 反思决策: "
          f"{state.reflection.decision if state.reflection else 'N/A'} "
          f"(置信度 {state.reflection.confidence if state.reflection else 'N/A'})\n")

    print("=== 工具执行轨迹 ===")
    for r in state.tool_results:
        ok = "✅" if r.status == "SUCCESS" else "❌"
        print(f"  {ok} {r.tool} ({r.execution_time_ms}ms) "
              f"artifacts={r.artifacts}")
        if not r.error:
            pass
        else:
            print(f"      错误: {r.error}")

    print("\n=== 业务分析报告 ===\n")
    print(state.report)


if __name__ == "__main__":
    main()
