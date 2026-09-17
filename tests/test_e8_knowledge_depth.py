"""E8/01 知识库深度（D57）——表格感知分块 + 索引版本 + 坏 chunk 回流。

红→绿顺序：
1. 先跑此文件 → 全红（方法/类尚未实现或行为未改造）。
2. 实现 `chunker.chunk_structured`、`KnowledgeStore.rebuild_source` / `chunk_diagnostics` 等 → 转绿。
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.etl.chunker import chunk_structured, chunk_text
from app.core.tools.knowledge_tool import KnowledgeStore


# ---------------------------------------------------------------------------
# 1. 表格感知分块
# ---------------------------------------------------------------------------

MARKDOWN_TABLE_DOC = """# 季度营收报告

以下是 2024 年各区域营收数据。

| 区域 | 营收(万元) | 增长率 |
|------|-----------|--------|
| 华东 | 1245 | +18.5% |
| 华南 | 890 | +12.3% |
| 华北 | 670 | +8.7% |
| 西部 | 320 | +5.2% |

总结：华东区域增长最快。
"""

HTML_TABLE_DOC = """<html><body><p>我们的渠道数据如下：</p>
<table>
<tr><th>渠道</th><th>UV</th></tr>
<tr><td>自然搜索</td><td>15000</td></tr>
<tr><td>付费推广</td><td>22000</td></tr>
<tr><td>社交媒体</td><td>8000</td></tr>
</table>
<p>以上是本月数据。</p></body></html>
"""


class TestTableAwareChunking:
    def test_markdown_table_rows_preserved_as_unit(self):
        """Markdown 表的每一行不可拆分：整行（含表头 context）在一个 chunk 里。"""
        chunks = chunk_structured(MARKDOWN_TABLE_DOC)
        # 表格 chunk：同时包含表头词"区域"与数据"华东"——避免误命中含"营收"的叙述段
        table_chunks = [c for c in chunks if "区域" in c and "华东" in c]
        assert len(table_chunks) >= 1, f"expected table chunks with header+data, got {chunks}"
        first = table_chunks[0]
        assert "区域" in first, f"header missing in {first!r}"
        assert "华东" in first, f"data row missing in {first!r}"
        # 整行不被切断：「1245」不能单独出现在没有「华东」的 chunk
        for c in table_chunks:
            if "1245" in c:
                assert "华东" in c, f"numeric cell orphaned without row context: {c!r}"

    def test_html_table_rows_preserved(self):
        """HTML table 的 <tr> 行不被切断。"""
        chunks = chunk_structured(HTML_TABLE_DOC)
        # 表格 chunk：必须包含具体数据行而非仅表前叙述（避免"渠道"误命中段落）
        table_chunks = [c for c in chunks if "自然搜索" in c]
        assert len(table_chunks) >= 1, f"expected HTML table chunks, got {chunks}"
        first = table_chunks[0]
        assert "渠道" in first, f"header missing in {first!r}"
        assert "自然搜索" in first
        assert "付费推广" in first or len(table_chunks) > 1

    def test_malformed_table_fallback(self):
        """畸形表（不匹配的列数、缺分隔行）→ 回退纯文本滑窗，不抛不丢。"""
        broken = "没表头直接来行\n| a | b |\n只一行也不成表\n\n正文正常段落在上。"
        # 不应抛错
        chunks = chunk_structured(broken)
        # 内容完整保留（纯文本滑窗长度 ≈ 原文去空白）
        joined = " ".join(chunks)
        assert "正文正常段落" in joined

    def test_non_table_text_unchanged(self):
        """纯文本走老路径：chunk_text 行为不变。"""
        plain = "这是第一段。\n\n这是第二段，长到超过 600 字符。" + "abcdefghij" * 80
        a = chunk_text(plain)
        b = chunk_structured(plain)
        assert a == b, "plain text must produce identical chunks via either entry"

    def test_table_prefix_injected(self):
        """紧邻表格前的非表文本作为语义前缀注入到行组 chunk。"""
        chunks = chunk_structured(MARKDOWN_TABLE_DOC)
        first = [c for c in chunks if "营收" in c][0]
        # 「季度营收报告」或「2024 年」这种叙述前缀要带进表格 chunk，
        # 让 BM25 能命中「报告」这类不出现表内的词
        assert "季度" in first or "2024" in first, f"narrative prefix missing: {first!r}"


# ---------------------------------------------------------------------------
# 2. 索引版本
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_kb(tmp_path):
    """独立的 KnowledgeStore（SQLite，临时文件）。"""
    return KnowledgeStore(db_path=tmp_path / "test_kb.db")


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    """本文件所有用例都不依赖真实嵌入模型。

    为什么必须打桩：``KnowledgeStore.add`` 在嵌入不可用时会把分块标成
    ``embed_failed``（``status != 'ok'``），而本文件有 ``diag["ok"] >= 1`` 这类断言。
    真实模型加载本机实测约 17s，而 ``embed_load_timeout_s`` 是 25s——边距只有 7 秒，
    机器无模型缓存、或与其他用例抢 CPU 时就会超时，于是这些**与嵌入无关**的用例
    随机变红（``chunk_diagnostics`` 校验的是分块质量门与统计，跟向量没关系）。

    返回确定性假向量后：分类仍是 ``ok``、``search`` 仍走真实混合检索路径，
    同时本文件从 ~18s 降到亚秒级。
    """
    from app.core.tools import knowledge_tool

    monkeypatch.setattr(knowledge_tool, "_embed", lambda _t: [0.1] * 8)


class TestIndexVersioning:
    def test_rebuild_source_no_dup(self, tmp_kb):
        """同内容二次 rebuild 不产生重复 chunk（内容 hash 去重）。"""
        src = "sop.txt"
        text = "第一行定义。\n第二行口径：GMV = 已支付订单金额。"
        v1 = tmp_kb.rebuild_source(src, text, tenant="default")
        assert v1["added"] >= 1
        v2 = tmp_kb.rebuild_source(src, text, tenant="default")
        # 第二次：添加数为 0（全部命中已有内容 hash）
        assert v2["added"] == 0, f"duplicate rows on 2nd rebuild: {v2}"
        total = tmp_kb.total_chunks()
        # 源文件 → 1 个 chunk（<600 字符）
        assert total == 1, f"expected 1 chunk, found {total}"

    def test_old_version_deprecated_excluded(self, tmp_kb):
        """rebuild 后旧版本标记 deprecated，不再出现在 search 结果里。"""
        src = "policy.md"
        v1_text = "原版：口径 X = 订单金额。"
        v2_text = "修订版：口径 X = 扣除退款后订单金额。"
        tmp_kb.rebuild_source(src, v1_text, tenant="default")
        hits_v1 = tmp_kb.search("口径", top_k=10, tenant="default")
        assert any("原版" in h["text"] for h in hits_v1), "v1 should be searchable"

        tmp_kb.rebuild_source(src, v2_text, tenant="default")
        hits_v2 = tmp_kb.search("口径", top_k=10, tenant="default")
        # 旧版（原版）不出现在搜索结果
        assert not any("原版" in h["text"] for h in hits_v2), \
            "deprecated chunk should be excluded from search"
        # 新版命中
        assert any("修订版" in h["text"] for h in hits_v2)

    def test_cleanup_old_versions(self, tmp_kb):
        """keep=2 时物理删除第三旧版本。"""
        src = "doc.md"
        tmp_kb.rebuild_source(src, "版本一：旧内容。", tenant="default")
        tmp_kb.rebuild_source(src, "版本二：新内容。", tenant="default")
        tmp_kb.rebuild_source(src, "版本三：最新内容。", tenant="default")

        # 验证 versions 计数的内部 helper
        remaining = tmp_kb.cleanup_old_versions(src, keep=2, tenant="default")
        assert remaining <= 2, f"expected ≤2 versions left, got {remaining}"


# ---------------------------------------------------------------------------
# 3. 坏 chunk 回流
# ---------------------------------------------------------------------------

class TestBadChunkFeedback:
    def test_empty_chunk_marked(self, tmp_kb):
        """空文本 strip 后零长度 → status='empty'，不出现在 search。"""
        tmp_kb.add("", "empty_src.txt", tenant="default")
        hits = tmp_kb.search("anything", top_k=10, tenant="default")
        assert not any(h["_source"] == "empty_src.txt" for h in hits)

    def test_noise_chunk_marked(self, tmp_kb):
        """纯标点 → status='noise'，不出现在 search。"""
        tmp_kb.add("...,,,!!!   \n\n  ", "noise_src.txt", tenant="default")
        hits = tmp_kb.search("anything", top_k=10, tenant="default")
        assert not any(h["source"] == "noise_src.txt" for h in hits)

    def test_chunk_diagnostics_reports_stats(self, tmp_kb):
        """chunk_diagnostics 返回 ok/empty/noise 统计。"""
        tmp_kb.add("这是正常的业务定义文本。", "good.md", tenant="default")
        tmp_kb.add("", "bad_empty.md", tenant="default")
        tmp_kb.add("!!!???", "bad_noise.md", tenant="default")

        diag = tmp_kb.chunk_diagnostics(tenant="default")
        assert diag["ok"] >= 1
        assert diag["empty"] >= 1
        assert diag["noise"] >= 1
