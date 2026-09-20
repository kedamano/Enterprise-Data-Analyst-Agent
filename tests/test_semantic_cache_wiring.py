"""流式入口 × 语义缓存的接线回归（真实缺陷）。

背景：``graph._stream_analysis_inner`` 里曾写成

    from .semantic_cache import enabled as _semantic_enabled, ...

但该模块的开关叫 ``semantic_enabled``（``enabled`` 是 ``response_cache`` 的名字）。
于是每次走 SSE（前端唯一入口）都在进入编排前抛 ``ImportError``，
被 ``stream_analysis`` 的兜网收敛成 ``status=ERROR``，用户只看到"流程异常终止"。
同步路径 ``run_analysis`` 名字是对的 → 同一功能两条路径一死一活，
UI 上完全看不出"只是导入名写错了"。

本文件用两层守住：
1. **端到端**：语义缓存开启（默认值）时流式跑通，不产出 ERROR 帧；
2. **静态契约**：把包内**所有**相对导入的符号逐个解析，确保它们真实存在——
   同类"改名 / 复制粘贴串名"错误不必等运行到那条分支才暴露。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "app" / "core" / "agents" / "data_analyst"


@pytest.fixture
def stream_env(monkeypatch, tmp_path):
    """确定性离线环境：MOCK_LLM + 独立 checkpoint 目录（不污染 data/）。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


# --------------------------------------------------------------------------- #
# 1) 端到端：语义缓存开启时，流式入口必须能正常进入编排
# --------------------------------------------------------------------------- #
def test_stream_enters_pipeline_when_semantic_cache_enabled(stream_env, monkeypatch, tmp_path):
    """默认配置（语义缓存开启）下，流式路径不得因缓存模块接线错误而整体失败。

    修复前这里产出的是首帧即 ``status=ERROR`` + ``ImportError``，
    且只在流式路径复现 —— 正是本测试要钉住的形状。
    """
    monkeypatch.setenv("SEMANTIC_CACHE_DB_PATH", str(tmp_path / "semantic_cache.db"))
    get_settings.cache_clear()

    from app.core.agents.data_analyst import graph, semantic_cache

    semantic_cache._reset_connection()
    semantic_cache.clear_semantic()

    # 前提断言：确认真的跑在"语义缓存开启"这条分支上，否则本测试是空转
    assert semantic_cache.semantic_enabled() is True, (
        "前提不成立：语义缓存被关掉了，本回归没有覆盖到目标分支"
    )

    snaps = list(graph.stream_analysis("sem_wiring", "统计各区域营收"))

    assert snaps, "至少要产出快照"
    last = snaps[-1]
    assert "ImportError" not in (last.error or ""), (
        f"流式入口仍然因导入失败而终止：{last.error}"
    )
    assert last.metadata.get("aborted_by_exception") != "ImportError", last.error
    assert last.metadata.get("aborted_by_exception") is None, (
        "流式流水线不应有异常逃逸到兜网；实际 aborted_by_exception="
        f"{last.metadata.get('aborted_by_exception')}，error={last.error}"
    )


# --------------------------------------------------------------------------- #
# 2) 静态契约：包内所有相对导入的符号都必须真实存在
# --------------------------------------------------------------------------- #
def _package_of(py: Path) -> str:
    """app/core/agents/data_analyst/graph.py → app.core.agents.data_analyst.graph"""
    return ".".join(py.relative_to(REPO_ROOT).with_suffix("").parts)


def _iter_relative_imports(py: Path) -> list[tuple[str, str | None, str]]:
    """收集该文件里所有「相对导入」的 ``(base_pkg, module, name)``。

    ``from .x import y``（level=1）指向**当前包**，``from ..x import y``（level=2）
    指向父包，依此类推；``module`` 为 ``None`` 表示 ``from . import y`` 这种写法。
    """
    tree = ast.parse(py.read_text(encoding="utf-8"))
    own_pkg = _package_of(py).rsplit(".", 1)[0].split(".")
    found: list[tuple[str, str | None, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        keep = len(own_pkg) - (node.level - 1)
        if keep <= 0:
            continue
        base = ".".join(own_pkg[:keep])
        for alias in node.names:
            if alias.name == "*":
                continue
            found.append((base, node.module, alias.name))
    return found


def _resolve_symbol(base: str, module: str | None, name: str) -> str:
    """返回 ``"ok"`` / ``"missing"`` / ``"skip"``（可选依赖未安装）。"""
    target = f"{base}.{module}" if module else base

    def _importable(dotted: str) -> bool:
        try:
            importlib.import_module(dotted)
            return True
        except Exception:
            return False

    if not _importable(target):
        # ``from . import sub``：target 是包，模块名落在 name 上
        if module is None and _importable(f"{base}.{name}"):
            return "ok"
        return "skip"

    mod = importlib.import_module(target)
    if hasattr(mod, name):
        return "ok"
    # ``from .pkg import submodule``：属性可能尚未挂上（父包 __init__ 未显式导入），
    # 此时能真的 import 到子模块就算成立 —— 不让结论依赖"先被别的测试导入过"
    if _importable(f"{target}.{name}"):
        return "ok"
    return "missing"


def test_intra_package_relative_imports_all_resolve():
    """``from .mod import name`` 里的 ``name`` 必须真实存在于 ``mod``。

    这类错误（改名/串名）只在**运行到那一行**时才炸，且往往被上层兜网
    收敛成一句笼统的 "流程异常终止"。静态扫一遍即可在秒级发现全部同类问题。
    """
    problems: list[str] = []
    skipped: list[str] = []
    checked = 0

    for py in sorted(PKG_DIR.rglob("*.py")):
        for base, module, name in _iter_relative_imports(py):
            target = f"{base}.{module}" if module else base
            verdict = _resolve_symbol(base, module, name)
            if verdict == "skip":
                skipped.append(f"{py.name}: from {target} import {name}（模块不可导入，跳过）")
                continue
            checked += 1
            if verdict == "missing":
                problems.append(f"{py.name}: from {target} import {name} → 符号不存在")

    # 防止"空集合相等"式假阳性：必须先证明真的校验到了一批导入
    assert checked > 0, f"没有校验到任何导入（样本为空，结论无效）。skipped={skipped}"
    assert not problems, (
        "相对导入的符号不存在（改名 / 复制粘贴串名的典型症状）：\n  "
        + "\n  ".join(problems)
    )
