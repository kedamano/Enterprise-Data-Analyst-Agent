"""将 SQLite 知识库（data/knowledge.db 的 chunks 表）迁移到 Milvus（Lite）。

前置条件（由调用方保证）：
- ``.env`` 的 ``EMBED_MODEL`` 已切到 ``BAAI/bge-small-zh-v1.5``（512 维）。
- bge 模型已下载到 HF 缓存（离线可加载）。
- **后端 uvicorn 已停止**——Milvus Lite 单进程独占锁，不释放迁移会 DataDirLockedError。

行为：
1. drop 现有 collection（旧维度 384），用新模型维度重建（512）。
2. 逐条读取 SQLite 中 ``status='ok'`` 且未废弃的分块，用 bge 重嵌并写入 Milvus。
3. 校验 Milvus ``total_chunks`` 与 SQLite 源分块数一致。

运行：
    .venv/Scripts/python.exe scripts/migrate_kb_to_milvus.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.core.tools.knowledge_tool import MilvusKnowledgeStore
from app.infrastructure.vectorstore.milvus import get_client

DB = ROOT / "data" / "knowledge.db"


def main() -> None:
    s = get_settings()
    print(f"[migrate] embed_model={s.embed_model}  collection={s.milvus_collection}")

    client = get_client()
    if client is None:
        raise SystemExit("Milvus client 不可用（检查 MILVUS_LITE_PATH 是否配置且未被其他进程占用）")
    col = s.milvus_collection

    if client.has_collection(col):
        print(f"[migrate] drop collection '{col}' (rebuild at new dim)")
        client.drop_collection(col)

    # 重建：维度来自当前 embed_model（bge -> 512）
    store = MilvusKnowledgeStore(client, col)
    print(f"[migrate] rebuilt collection, dim={store.dim}")

    con = sqlite3.connect(str(DB))
    try:
        rows = con.execute(
            "SELECT text, source, kb_id FROM chunks "
            "WHERE deprecated=0 AND status='ok'"
        ).fetchall()
    finally:
        con.close()
    print(f"[migrate] source rows (SQLite ok chunks): {len(rows)}")

    added = 0
    for text, source, kb_id in rows:
        added += store.add(text, source, tenant=None, kb_id=kb_id)

    total = store.total_chunks()
    print(f"[migrate] vectors inserted={added}  Milvus total_chunks={total}")
    if total != len(rows):
        print(f"[migrate][WARN] total_chunks {total} != source {len(rows)} —— 部分写入失败")
        raise SystemExit(1)
    print("[migrate] OK: 计数一致，迁移完成")


if __name__ == "__main__":
    main()
