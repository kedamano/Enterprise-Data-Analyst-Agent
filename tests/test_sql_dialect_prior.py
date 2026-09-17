"""E2-04：把「引擎是什么」在**写之前**告诉 planner（预防），而不是等它写错再拦（检测）。

动因（D55 收官清点，2026-09-15）
--------------------------------
D54 §四 提了两个抓手，D55 只做了**执行器侧**（检测 + 回灌）：

| 抓手 | 状态 |
|---|---|
| ① 失败时把方言提示 + 真实列清单回灌给 replan | ✅ D55（`E2/03`） |
| ② planner 提示词里明确执行引擎是 SQLite | ❌ **没做** |

`grep -ni "sqlite\\|DATE_TRUNC\\|方言" app/core/prompts/data_analyst/planner.md` → **零命中**。
于是真实基线里的链路是「写 Postgres 方言 → 预检拦下 → REPLAN → 重写」——
**每一轮都要先失败一次**。预检治的是**已发生的**，缺的是「别一上来就写错」。

**但本卡最关键的纪律是「不许硬编码 SQLite」**：D55 已经明确
「方言提示必须看引擎」（E7 多源下 `input.source` 可能指向真 PG 库，那里
`DATE_TRUNC` 是**原生**写法）。提示词是同一条知识的**另一个出口**——
写死 "禁止 DATE_TRUNC" 就会在 PG 源上**禁止模型用它能用的写法**，
而预检那边**根本不拦**（按引擎分级，对 PG 返回 `[]`）→ **两边口径相反**。

本文件钉住：分引擎的措辞 + 引擎从配置读 + 判不出就不说 + 绝不泄 DSN。
"""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst import nodes
from app.core.agents.data_analyst.sql_precheck import dialect_brief, dialect_hints
from app.core.agents.data_analyst.state import AgentState, ContextModel, PlanModel, PlanStep


# --------------------------------------------------------------------------- #
# 一、`dialect_brief`：分方向的措辞，未知引擎不猜
# --------------------------------------------------------------------------- #
def test_sqlite_brief_names_the_banned_constructs_and_the_replacements():
    text = dialect_brief("sqlite")
    assert "DATE_TRUNC" in text and "DATE_FORMAT" in text and "INTERVAL" in text
    assert "strftime" in text, "只说不许什么、不说该用什么，模型只能自己猜"
    assert "CAST" in text, "`x::type` 也只有 PG 认，必须给出替代写法"


def test_postgres_brief_says_those_constructs_are_fine():
    """**反方向**：PG 源上 `DATE_TRUNC` 是原生写法，提示词不许禁止它。

    只给 SQLite 的"禁令清单"等于换个方向犯同一个错——
    与 `dialect_hints(sql, engine="postgres") == []` 是同一条纪律的两个出口。
    """
    text = dialect_brief("postgres")
    assert "DATE_TRUNC" in text
    assert "可以" in text or "原生" in text, (
        "PG 的要点必须说清这些构造**能用**，否则模型会避开它能用的原生写法"
    )


def test_mysql_brief_is_about_mysql_not_sqlite():
    text = dialect_brief("mysql")
    assert "DATE_FORMAT" in text, "MySQL 的原生日期格式化是 DATE_FORMAT"
    assert ("原生" in text) or ("可以" in text), "必须说清它在 MySQL 上能用"
    assert "strftime" not in text, "MySQL 没有 strftime——写进去就是教模型编函数"


@pytest.mark.parametrize("engine", [None, "", "unknown", "  ", "oracle", "hive"])
def test_unknown_engine_says_nothing(engine):
    """判不出来就**不说**——比说错好。（猜一个引擎 = 把先验变成误导。）"""
    assert dialect_brief(engine) == ""


# --------------------------------------------------------------------------- #
# 二、与 `dialect_hints` 同源：单向漂移守卫
# --------------------------------------------------------------------------- #
_BANNED_ON_SQLITE = {
    "DATE_TRUNC": "SELECT DATE_TRUNC('month', d) FROM t",
    "DATE_FORMAT": "SELECT DATE_FORMAT(d, '%Y-%m') FROM t",
    "INTERVAL": "SELECT d + INTERVAL '1 month' FROM t",
    "CURRENT_DATE": "SELECT * FROM t WHERE d > CURRENT_DATE",
    "::": "SELECT d::date FROM t",
}


@pytest.mark.parametrize("token,sql", list(_BANNED_ON_SQLITE.items()))
def test_everything_the_precheck_catches_is_also_warned_about_in_the_brief(token, sql):
    """预检抓得到的构造，先验里**必须点名**。

    否则会出现「模型写出了预检会拦的东西、但提示词从没提醒过它」——
    那么每一轮都要靠一次失败来教它，正是本卡要消掉的那个成本。
    这是**单向**守卫（预检 ⊇ 先验）：足够抓住「新加了预检规则、忘了同步先验」。
    """
    assert dialect_hints(sql, engine="sqlite"), f"{token} 没被预检识别（用例本身过期了）"
    assert token in dialect_brief("sqlite"), f"{token} 被预检拦，但先验里没提"


def test_the_brief_does_not_recommend_something_the_precheck_rejects():
    """**推荐的写法不许被自己的预检拦下**——否则模型照做一次就被拦一次，转圈。"""
    assert dialect_hints(
        "SELECT strftime('%Y-%m', d) AS m, CAST(x AS INTEGER) FROM t GROUP BY 1",
        engine="sqlite") == []


# --------------------------------------------------------------------------- #
# 三、`_dialect_text`：引擎从**配置**读，而不是写死
# --------------------------------------------------------------------------- #
def _state() -> AgentState:
    s = AgentState(session_id="dlx", user_query="按区域看营收")
    s.context = ContextModel(objective="按区域看营收", metrics=["营收"])
    return s


def test_dialect_text_reads_the_configured_engine():
    text = nodes._dialect_text(_state())
    assert "sqlite" in text.lower()
    assert "default" in text, "要让模型知道这是**主源**"


def test_dialect_text_never_leaks_the_dsn():
    """只给**源名与方言**，绝不回连接串（与 `available_sources()` 的既有约定一致）。"""
    text = nodes._dialect_text(_state())
    assert "sqlite:///" not in text
    assert "sample_enterprise.db" not in text


def test_dialect_text_fails_open(monkeypatch):
    """读配置失败 → 空串（→ 调用方不注入键）→ **planner 照常工作**，绝不崩。"""
    from app.core.tools import datasource

    def _boom():
        raise RuntimeError("配置读不到")

    monkeypatch.setattr(datasource, "sources", _boom)
    assert nodes._dialect_text(_state()) == ""


def test_dialect_text_lists_multiple_sources_with_opposite_advice(monkeypatch):
    """多源：两个源**各自的要点方向相反**（SQLite 禁、PG 许）——这是本卡的核心价值。"""
    from app.config import get_settings
    from app.core.tools import datasource

    monkeypatch.setattr(datasource, "sources", lambda: {
        "default": {"url": "sqlite:///./data/sample_enterprise.db", "dialect": "sqlite"},
        "pg_warehouse": {"url": "postgresql://u:p@h/db", "dialect": "postgresql"},
    })
    monkeypatch.setattr(datasource, "available_sources", lambda: ["default", "pg_warehouse"])
    get_settings.cache_clear()

    text = nodes._dialect_text(_state())
    assert "pg_warehouse" in text and "postgres" in text.lower()
    # PG 那一行必须与 SQLite 那一行**方向相反**
    pg_line = next(ln for ln in text.splitlines() if "pg_warehouse" in ln)
    assert "DATE_TRUNC" in pg_line
    assert "strftime" not in pg_line, "把 SQLite 的替代写法安到 PG 源上，方向就反错了"
    assert "postgresql://" not in text


def test_dialect_text_is_bounded(monkeypatch):
    """有界：源再多也不能把 prompt 预算吃光。"""
    from app.core.tools import datasource

    many = {f"src_{i}": {"url": f"postgresql://h/{i}", "dialect": "postgresql"}
            for i in range(60)}
    monkeypatch.setattr(datasource, "sources", lambda: many)
    monkeypatch.setattr(datasource, "available_sources", lambda: list(many))
    text = nodes._dialect_text(_state())
    assert len(text) < 4000, f"方言段落无界（{len(text)} 字符）"
    assert text.count("\n") < 30


# --------------------------------------------------------------------------- #
# 四、接线：`run_planner` 的 payload
# --------------------------------------------------------------------------- #
def _capture_planner_payload(monkeypatch) -> str:
    captured: dict = {}

    def _fake_llm(model_cls, stage, user, **kw):
        captured["user"] = user
        return PlanModel(goal="g", steps=[PlanStep(
            id="step_1", objective="o", action="a", tool="schema_search")]), None

    monkeypatch.setattr(nodes, "_llm_model", _fake_llm)
    nodes.run_planner(_state())
    return captured["user"]


def test_planner_payload_carries_the_dialect(monkeypatch):
    payload = _capture_planner_payload(monkeypatch)
    assert "sql_dialect" in payload, (
        "planner 完全不知道自己要写哪个引擎的 SQL —— 它只能按训练语料里最常见的方言写"
    )
    assert "sqlite" in payload.lower()
    assert "strftime" in payload, "只给引擎名不够，要给**可执行的替代写法**"


def test_planner_payload_does_not_leak_the_dsn(monkeypatch):
    payload = _capture_planner_payload(monkeypatch)
    body = payload.split("<task_context>", 1)[1]
    assert "sqlite:///" not in body and "sample_enterprise.db" not in body


def test_planner_omits_the_key_when_engine_is_unknown(monkeypatch):
    """判不出引擎 → **不注入空壳键**（与 `discovered_schema` 的既有纪律一致）。"""
    monkeypatch.setattr(nodes, "_dialect_text", lambda state: "")
    payload = _capture_planner_payload(monkeypatch)
    assert "sql_dialect" not in payload


def test_planner_prompt_documents_the_dialect_rule():
    from app.core.prompts import load_prompt

    text = load_prompt("planner")
    assert "sql_dialect" in text, "上下文里给了键，提示词里却没说怎么用"
    assert "discovered_schema" in text


def test_planner_prompt_covers_the_unknown_engine_case():
    """键缺失时的兜底必须写在提示词里：**最保守的 SQL**，而不是随便挑一个方言。"""
    from app.core.prompts import load_prompt

    text = load_prompt("planner")
    assert "DATE_TRUNC" in text and "INTERVAL" in text, (
        "没给出兜底时，模型在没有 sql_dialect 的上下文里没有任何依据"
    )
    assert "CAST" in text, "保守写法必须给出可用替代，否则只是一串禁令"
