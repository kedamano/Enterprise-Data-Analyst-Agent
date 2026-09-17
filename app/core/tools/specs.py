"""Tool Permission Model (spec §22).

Every tool must declare a ``ToolSpec``: permission, timeout, data scope, rate
limit and audit policy. The vocabulary is deliberately read/compute only —
there is no WRITE permission; the default posture is ``NO WRITE ACCESS``
(hard write-rejection lives in ``sql_tool`` / ``python_tool``).

``execute_tool`` enforces the declared policy:
* rate limiting per (session, tool) — sliding window, ``rate_limit_per_min``;
* audit logging (``audit_policy=ALWAYS``) — one JSONL record per execution,
  success and failure alike, in ``data/audit/tool_audit.jsonl``.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field


class ToolPermission(str, Enum):
    READ_METADATA = "READ_METADATA"      # schema_search
    READ_KNOWLEDGE = "READ_KNOWLEDGE"    # knowledge_search
    READ_DATA = "READ_DATA"              # dataset_profile / sql_query
    COMPUTE = "COMPUTE"                  # python_analysis / visualization
    GENERATE_ARTIFACT = "GENERATE_ARTIFACT"  # generate_report
    # 注意：词表中不存在 WRITE 权限——规格 §22 默认 NO WRITE ACCESS


class ToolSpec(BaseModel):
    """Declarative per-tool contract (spec §22)."""

    name: str
    description: str
    permission: ToolPermission
    timeout_s: int
    data_scope: str
    rate_limit_per_min: int
    audit_policy: str = "ALWAYS"  # ALWAYS | NEVER
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# AUTH/01 §6 RBAC：角色 → 权限映射（最小权限原则）
#
# 企业面试几乎必问"谁能调哪些工具 / 看哪些数据"。这里把抽象的「角色」落到
# 具体的 ToolPermission 上，工具层在 `execute_tool` 执行前据此门禁。
#   - viewer        : 只读元数据 + 知识库（不能碰业务数据）
#   - analyst       : + 读业务数据 + 计算（沙箱 Python / 可视化）
#   - analyst_lead  : + 生成报告（GENERATE_ARTIFACT）
#   - admin         : 全部权限
# 未知角色被忽略（安全阀：配置写错不会意外放大权限）。
# --------------------------------------------------------------------------- #
ROLE_PERMISSIONS: dict[str, set["ToolPermission"]] = {
    "viewer": {
        ToolPermission.READ_METADATA,
        ToolPermission.READ_KNOWLEDGE,
    },
    "analyst": {
        ToolPermission.READ_METADATA,
        ToolPermission.READ_KNOWLEDGE,
        ToolPermission.READ_DATA,
        ToolPermission.COMPUTE,
    },
    "analyst_lead": {
        ToolPermission.READ_METADATA,
        ToolPermission.READ_KNOWLEDGE,
        ToolPermission.READ_DATA,
        ToolPermission.COMPUTE,
        ToolPermission.GENERATE_ARTIFACT,
    },
    "admin": set(ToolPermission),
}


def roles_to_permissions(roles: Iterable[str]) -> set["ToolPermission"]:
    """把角色列表展开为权限集合（并集）。未知角色忽略。"""
    out: set["ToolPermission"] = set()
    for r in roles or []:
        out |= ROLE_PERMISSIONS.get(str(r), set())
    return out


TOOL_SPECS: dict[str, ToolSpec] = {
    "schema_search": ToolSpec(
        name="schema_search",
        description="Search available enterprise datasets, tables, columns and relationships.",
        permission=ToolPermission.READ_METADATA,
        timeout_s=10,
        data_scope="metadata:information_schema",
        rate_limit_per_min=60,
        input_schema={
            "type": "object",
            "properties": {
                # `query` 是 `keyword` 的别名（历史契约）；实现两者都读。
                "query": {"type": "string"},
                "keyword": {"type": "string"},
                "entities": {"type": "array", "items": {"type": "string"}},
                # E7/02：命名数据源（缺省 = 主源；主源零命中会自动扫描其余源）
                "source": {"type": "string"},
            },
            # 校正：实现只按关键词过滤、且零命中会放宽成候选表，故无需必填
            "required": [],
        },
    ),
    "knowledge_search": ToolSpec(
        name="knowledge_search",
        description="Search enterprise business knowledge and data definitions.",
        permission=ToolPermission.READ_KNOWLEDGE,
        timeout_s=15,
        data_scope="knowledge:da_knowledge",
        rate_limit_per_min=60,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    ),
    "dataset_profile": ToolSpec(
        name="dataset_profile",
        description="Profile dataset quality and structure.",
        permission=ToolPermission.READ_DATA,
        timeout_s=30,
        data_scope="data:analytical_store",
        rate_limit_per_min=30,
        input_schema={
            "type": "object",
            "properties": {
                "dataset": {"type": "string"},
                "table": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "sample_rows": {"type": "integer", "default": 1000},
            },
            "required": ["dataset"],
        },
    ),
    "sql_query": ToolSpec(
        name="sql_query",
        description="Execute read-only SQL against approved enterprise data sources.",
        permission=ToolPermission.READ_DATA,
        timeout_s=30,
        data_scope="data:analytical_store(read-only)",
        rate_limit_per_min=30,
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "database": {"type": "string"},
                # E7/02：命名数据源（缺省 = 主源）；实现读 `source`
                "source": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 30},
                "max_rows": {"type": "integer", "default": 10000},
            },
            "required": ["sql", "database"],
        },
    ),
        "freeform": ToolSpec(
        name="freeform",
        description="Execute analyst-authored read-only SQL under the same guardrails as sql_query.",
        permission=ToolPermission.READ_DATA,
        timeout_s=60,
        data_scope="enterprise-store:readonly",
        rate_limit_per_min=20,
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    ),
    "python_analysis": ToolSpec(

        name="python_analysis",
        description="Execute analytical Python code in an isolated sandbox.",
        permission=ToolPermission.COMPUTE,
        timeout_s=60,
        data_scope="sandbox:per-session-workdir",
        rate_limit_per_min=20,
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "dataset_refs": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "integer", "default": 60},
            },
            "required": ["code"],
        },
    ),
    "visualization": ToolSpec(
        name="visualization",
        description="Generate analytical visualizations from validated datasets.",
        permission=ToolPermission.COMPUTE,
        timeout_s=30,
        data_scope="sandbox:per-session-workdir",
        rate_limit_per_min=30,
        input_schema={
            "type": "object",
            "properties": {
                "dataset_ref": {"type": "string"},
                "chart_type": {"type": "string"},
                "x": {"type": "string"},
                "y": {"type": "array", "items": {"type": "string"}},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "title": {"type": "string"},
            },
            "required": ["dataset_ref", "chart_type"],
        },
    ),
    "generate_report": ToolSpec(
        name="generate_report",
        description="Generate a business-facing report from validated analysis results.",
        permission=ToolPermission.GENERATE_ARTIFACT,
        timeout_s=30,
        data_scope="artifact:session-report",
        rate_limit_per_min=20,
        input_schema={
            "type": "object",
            "properties": {
                "analysis_result": {"type": "object"},
                "format": {"type": "string", "enum": ["markdown", "html", "pdf", "docx"]},
            },
            "required": ["analysis_result", "format"],
        },
    ),
    "image_analyze": ToolSpec(
        name="image_analyze",
        description="识别用户上传图片（图表/仪表盘/表格截图）中的数据、文字与趋势，作为分析证据。",
        permission=ToolPermission.READ_DATA,
        timeout_s=60,
        data_scope="image:user_upload",
        rate_limit_per_min=20,
        input_schema={
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "上传图片的文件名"},
                "question": {"type": "string", "description": "希望从图片中提取的信息"},
            },
            "required": ["image"],
        },
    ),
}

# --------------------------------------------------------------------------- #
# 不能作为**计划步骤**的工具（E2/05）
#
# 理由只有一个：**流水线顺序**。计划步骤由 Executor 执行，而 Executor 在 Analyst
# **之前**（`graph.py:161` vs `graph.py:172`）。`generate_report` 的唯一输入是
# `state.analysis`（`report_tool.run` 只读 analysis/reflection/objective），
# 那一刻它还是空值 → 该步骤要么被依赖级联跳过，要么侥幸执行后渲染一份**空壳报告**。
# D54 真实基线（`data/checkpoints/eval_*.json`）：13 次跳过 + 1 次 163 字符空壳，
# 零 findings / 零指标 / 零建议。
#
# 它**不是**"坏工具"：`run_reporter` 仍用它做模板兜底，直接调用也仍然可用
# （`tests/test_agent_real.py::test_generate_report_runs`）。禁的是**把它排进计划**。
#
# 这份名单是**唯一来源**：planner 侧（`routing.select_tools_for_planner`）与
# 执行器侧（`nodes._run_one_step`）都读它——加一个名字，两侧同时生效（防漂移）。
# --------------------------------------------------------------------------- #
NOT_PLANNABLE_TOOLS: frozenset[str] = frozenset({"generate_report"})


# --------------------------------------------------------------------------- #
# Rate limiting — sliding window per (session, tool)
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Thread-safe sliding-window limiter keyed by (session_id, tool)."""

    def __init__(self) -> None:
        self._calls: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: tuple[str, str], limit_per_min: int) -> bool:
        now = time.monotonic()
        with self._lock:
            window = self._calls[key]
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= limit_per_min:
                return False
            window.append(now)
            return True


AUDIT_LOG = Path("data/audit/tool_audit.jsonl")
