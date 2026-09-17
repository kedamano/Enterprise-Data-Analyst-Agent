"""Agent-core 对外暴露的"端口"接口。

层污染治理（A / 2026-09-17）
=============================
历史问题：``agents/data_analyst/graph.py`` 直接 import
    ``infrastructure.observability.tracing.trace_run``
    ``infrastructure.llm.router.fallback_events``
让 agent/orchestration 层反向耦合到基础设施层。

本模块做法并不引入"接口-实现"的 Python ABC/Protocol——本项目的 infrastructure
实现在**进程内**已经是 singleton，再叠 protocol 是过度设计。这里只做一件事：
**把 graph.py / iteration.py / 其他 core/ 调用方的 import 入口收敛到本文件**，
把 infrastructure 的真实调用链锁在 interfaces.py 内。

- 想换 Anthropic SDK / 换 OpenTelemetry 后端 / 把事件落地到 Kafka：
  只改 interfaces.py 一行 import + infrastructure/ 内部，core/ 零改动。
- 想在单测里 stub trace_run / fallback_events：
  直接 ``mock.patch("app.core.interfaces.trace_run", ...)``。

纪律：本文件**只 re-export core 需要的符号**。不要把它当成"全部 infrastructure 的转发表"。
"""
from __future__ import annotations

# 每个新符号都要加一行 re-export + 注释用途，方便 grep。

# --- observability ----------------------------------------------------------- #
# 让 orchestration 层给一次 run 打上 trace_id —— 没必要直接依赖 tracing 模块。
from app.infrastructure.observability.tracing import trace_run  # noqa: F401

# --- LLM 降级可见 ----------------------------------------------------------- #
# 让 orchestration 层读到本轮 fallback 事件，对外透出 degraded=True。
from app.infrastructure.llm.router import fallback_events  # noqa: F401
