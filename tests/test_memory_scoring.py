"""INTERVIEW/01 ① 记忆三因子打分：相关性 / 时效 / 重要性（八股文 05.6）。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §1
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.memory import long_term
from app.core.memory.scoring import (
    importance,
    rank,
    recency,
    relevance,
    score_entry,
    tokenize,
)
from app.infrastructure.llm.router import reset_llm

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture
def mem_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("POSTGRES_DSN", "")
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "lt.jsonl"))
    get_settings.cache_clear()
    reset_llm()
    yield tmp_path
    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 1. 三个因子各自的行为
# --------------------------------------------------------------------------- #
def test_tokenize_handles_chinese_and_english():
    t = tokenize("营收 Revenue 下滑")
    assert "营收" in t and "revenue" in t and "下滑" in t


def test_relevance_prefers_matching_entry():
    q = "华东区域营收下滑原因"
    near = {"text": "华东区域营收下滑主要来自渠道变化"}
    far = {"text": "库存周转天数与补货周期"}
    assert relevance(near, q) > relevance(far, q)
    assert relevance(far, q) == 0.0


def test_recency_decays_and_missing_ts_is_neutral():
    fresh = {"ts": NOW - 1 * DAY}
    old = {"ts": NOW - 180 * DAY}
    assert recency(fresh, now=NOW) > recency(old, now=NOW) > 0
    assert recency({"ts": NOW - 30 * DAY}, now=NOW) == pytest.approx(0.3679, abs=1e-3), \
        "半衰期 30 天 → 一个月前约为 e^-1"
    # 没有时间戳的旧条目：中性，不被判过期也不占便宜
    assert recency({}, now=NOW) == 0.5
    assert recency({"ts": "not-a-date"}, now=NOW) == 0.5


def test_recency_accepts_iso_string():
    from datetime import datetime, timezone

    iso = datetime.fromtimestamp(NOW - 5 * DAY, tz=timezone.utc).isoformat()
    assert recency({"ts": iso}, now=NOW) == pytest.approx(recency({"ts": NOW - 5 * DAY}, now=NOW))


def test_importance_by_type_and_confidence():
    assert importance({"type": "lesson"}) > importance({"type": "analysis_summary"})
    assert importance({"type": "analysis_summary"}) > importance({"type": "unknown"})
    # 低置信度要打折，但不至于归零
    low = importance({"type": "lesson", "confidence": 0.0})
    high = importance({"type": "lesson", "confidence": 1.0})
    assert 0 < low < high <= 1.0
    assert importance({"importance": 0.42}) == 0.42, "显式 importance 优先"


# --------------------------------------------------------------------------- #
# 2. 三因子合成的关键性质（八股文的"追问点"）
# --------------------------------------------------------------------------- #
def test_fresh_relevant_beats_stale_relevant():
    q = "营收下滑原因"
    fresh = {"text": "营收下滑原因：渠道结构变化", "ts": NOW - 2 * DAY, "type": "analysis_summary"}
    stale = {"text": "营收下滑原因：促销力度下降", "ts": NOW - 365 * DAY, "type": "analysis_summary"}
    assert score_entry(fresh, q, now=NOW) > score_entry(stale, q, now=NOW)


def test_high_relevance_beats_mere_recency():
    """**关键取舍**：昨天的一条无关摘要，不该压过三个月前的高相关结论。"""
    q = "华东区域营收下滑"
    relevant = {"text": "华东区域营收下滑由退货率上升导致", "ts": NOW - 90 * DAY}
    recent_irrelevant = {"text": "本周数据源连接正常，无异常", "ts": NOW - 1 * DAY}
    assert score_entry(relevant, q, now=NOW) > score_entry(recent_irrelevant, q, now=NOW)


def test_lesson_outranks_summary_at_equal_relevance_and_age():
    q = "库存积压"
    lesson = {"text": "库存积压的口径坑", "ts": NOW - 10 * DAY, "type": "lesson"}
    summary = {"text": "库存积压的口径坑", "ts": NOW - 10 * DAY, "type": "analysis_summary"}
    assert score_entry(lesson, q, now=NOW) > score_entry(summary, q, now=NOW)


def test_rank_is_stable_and_topk():
    q = "营收"
    entries = [
        {"id": "a", "text": "营收下滑", "ts": NOW - 1 * DAY},
        {"id": "b", "text": "营收增长", "ts": NOW - 1 * DAY},
        {"id": "c", "text": "无关内容", "ts": NOW - 1 * DAY},
    ]
    first = [e["id"] for e in rank(entries, q, top_k=2, now=NOW)]
    second = [e["id"] for e in rank(list(reversed(entries)), q, top_k=2, now=NOW)]
    assert first == second, "同分时必须稳定排序（否则检索结果不可复现）"
    assert "c" not in first, "低相关条目不该进 top-k"


# --------------------------------------------------------------------------- #
# 3. 落库：append 补 ts + search 用三因子排序 + 租户隔离不变
# --------------------------------------------------------------------------- #
def test_append_stamps_ts(mem_env):
    long_term.append({"type": "lesson", "text": "x"})
    raw = Path(get_settings().long_term_path).read_text(encoding="utf-8").strip()
    entry = json.loads(raw.splitlines()[-1])
    assert entry.get("ts"), "落库必须带时间戳，否则时效因子永远中性"


def test_search_ranks_by_three_factors(mem_env):
    """**两条都含查询子串**，只有三因子排序才会把「更新且同样相关」的排前面。

    （旧实现是子串匹配 + 文件顺序/时间倒序：先 append 的会先返回，
    所以这条用例在旧实现下必然失败——它真的在测排序，不是碰巧通过。）
    """
    long_term.append({"type": "analysis_summary", "text": "华东区域营收下滑由退货率上升导致",
                      "ts": time.time() - 90 * DAY})
    long_term.append({"type": "analysis_summary", "text": "华东区域营收下滑趋势与去年同期对比",
                      "ts": time.time() - 1 * DAY})

    hits = long_term.search("华东区域营收下滑", top_k=2)
    assert len(hits) == 2, hits
    assert "趋势与去年同期" in hits[0]["text"], f"更近的同等相关结论应排第一：{hits}"
    assert "退货率" in hits[1]["text"], "高相关但较旧的仍要在榜（不该被丢掉）"


def test_old_high_relevance_still_beats_recent_irrelevant(mem_env):
    long_term.append({"type": "analysis_summary", "text": "华东区域营收下滑由退货率上升导致",
                      "ts": time.time() - 90 * DAY})
    long_term.append({"type": "analysis_summary", "text": "本周数据源连接正常，无异常",
                      "ts": time.time() - 1 * DAY})
    hits = long_term.search("华东区域营收下滑", top_k=1)
    assert hits and "退货率" in hits[0]["text"], hits


def test_search_keeps_tenant_isolation(mem_env, monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT", "acme")
    get_settings.cache_clear()
    long_term.append({"type": "lesson", "text": "营收口径甲"}, tenant="acme")
    long_term.append({"type": "lesson", "text": "营收口径乙"}, tenant="other")
    hits = long_term.search("营收口径")
    assert hits and all(h.get("tenant") == "acme" for h in hits), hits


def test_search_returns_empty_on_empty_store(mem_env):
    assert long_term.search("任何问题") == []
