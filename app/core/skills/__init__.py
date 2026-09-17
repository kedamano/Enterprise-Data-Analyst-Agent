"""技能（Skills）子系统：把"分析方法论 / 口径 / 领域知识"打包成可勾选的提示词片段。

对外只暴露 ``get_skill_store`` 与 ``render_skills`` 两个入口，其余为内部实现。
"""
from __future__ import annotations

from .store import SkillStore, get_skill_store, render_skills

__all__ = ["SkillStore", "get_skill_store", "render_skills"]
